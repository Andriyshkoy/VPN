#!/usr/bin/env python3
"""Private online backups of control-API metadata, not VPN backend databases.

Restoring this archive does not restore Marzban or Hysteria/Mongo accounts.
No archive is extracted into a running service; --extract-to requires a new
directory and is only a staging step for an operator-managed restoration.
"""
import argparse
from contextlib import closing
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import time
from urllib.parse import quote

SCHEMA = "vpn-control-backup/v1"
MANIFEST = "backup-manifest.json"
LIMITS = {"control.sqlite3": 32 * 1024 * 1024, "tokens.json": 65536,
          "deploy.json": 1024 * 1024, "node.env": 65536,
          "hysteria-peer.crt": 65536, MANIFEST: 65536}
MAX_ARCHIVE = 40 * 1024 * 1024
MAX_TAR = sum(LIMITS.values()) + 128 * 1024
ARCHIVE_NAME = re.compile(r"vpn-control-(\d{8}T\d{6}Z)-[0-9a-f]{12}\.tar\.gz\Z")


class BackupError(Exception):
    """Only fixed, non-secret codes may reach command output."""


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for part in iter(lambda: stream.read(65536), b""):
            h.update(part)
    return h.hexdigest()


def private_directory(path, *, new=False):
    path = Path(path).absolute()
    if any(part == ".." for part in path.parts):
        raise BackupError("unsafe_directory")
    for parent in (*reversed(path.parents), path):
        if parent.exists() and parent.is_symlink():
            raise BackupError("symlink_directory")
    try:
        path.mkdir(mode=0o700, parents=not new, exist_ok=not new)
    except FileExistsError:
        raise BackupError("destination_exists") from None
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise BackupError("directory_not_owned")
    path.chmod(0o700)
    return path


def read_bounded(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise BackupError("source_size_or_type")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise BackupError("source_size_limit")
    return data


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def validate_tokens(data):
    try:
        value = json.loads(data)
        assert set(value) == {"tokens"} and isinstance(value["tokens"], list)
        assert value["tokens"]
        for token in value["tokens"]:
            assert isinstance(token, dict) and set(token) == {"id", "sha256", "scopes"}
            assert isinstance(token["id"], str) and 1 <= len(token["id"]) <= 128
            assert re.fullmatch(r"[0-9a-fA-F]{64}", token["sha256"])
            assert isinstance(token["scopes"], list) and all(isinstance(x, str) for x in token["scopes"])
    except (AssertionError, KeyError, ValueError, TypeError):
        raise BackupError("invalid_hashed_token_catalog") from None


def check_database(path):
    with closing(sqlite3.connect("file:" + quote(str(Path(path).absolute()), safe="/") + "?mode=ro&immutable=1", uri=True)) as db:
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise BackupError("sqlite_integrity_failed")


def online_database(source, destination):
    info = Path(source).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > LIMITS["control.sqlite3"]:
        raise BackupError("database_size_or_type")
    deadline = time.monotonic() + 45
    page_size = 4096
    def progress(_status, remaining, total):
        if time.monotonic() >= deadline or total * page_size > LIMITS["control.sqlite3"]:
            raise BackupError("database_backup_limit")
    uri = "file:" + quote(str(Path(source).absolute()), safe="/") + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as old:
        page_size = old.execute("PRAGMA page_size").fetchone()[0]
        if old.execute("PRAGMA page_count").fetchone()[0] * page_size > LIMITS["control.sqlite3"]:
            raise BackupError("database_size_limit")
        with closing(sqlite3.connect(destination)) as new:
            old.backup(new, pages=64, progress=progress, sleep=.05)
            # The backup must be a standalone database, even when the live
            # source uses WAL. No sidecar files belong in the archive.
            new.execute("PRAGMA journal_mode=DELETE")
    Path(destination).chmod(0o600)
    if Path(destination).stat().st_size > LIMITS["control.sqlite3"]:
        raise BackupError("database_size_limit")
    check_database(destination)


def retain(directory, *, days=14, minimum=2):
    candidates = []
    for path in Path(directory).iterdir():
        match = ARCHIVE_NAME.fullmatch(path.name)
        if match and path.is_file() and not path.is_symlink():
            stamp = dt.datetime.strptime(match[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
            candidates.append((stamp, path))
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    deleted = 0
    for index, (stamp, path) in enumerate(sorted(candidates, reverse=True)):
        if index >= minimum and stamp < cutoff:
            path.unlink()
            deleted += 1
    return deleted


def create(database=Path("/var/lib/vpn-control/control.sqlite3"),
           tokens=Path("/etc/vpn-control/tokens.json"),
           deployment=Path("/opt/vpn-control/deploy.json"),
           directory=Path("/var/backups/vpn-control"),
           node_config=Path("/etc/vpn-control/node.env"),
           peer_certificate=Path("/etc/vpn-control/hysteria-peer.crt")):
    os.umask(0o077)
    directory = private_directory(directory)
    stamp = dt.datetime.now(dt.timezone.utc)
    name = "vpn-control-" + stamp.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(6) + ".tar.gz"
    archive = directory / name
    pending = directory / ("." + name + ".tmp")
    try:
        with tempfile.TemporaryDirectory(prefix=".working-", dir=directory) as folder:
            work = Path(folder)
            work.chmod(0o700)
            online_database(database, work / "control.sqlite3")
            data = read_bounded(tokens, LIMITS["tokens.json"])
            validate_tokens(data)
            write_private(work / "tokens.json", data)
            if Path(deployment).exists():
                write_private(work / "deploy.json", read_bounded(deployment, LIMITS["deploy.json"]))
            if Path(node_config).exists():
                write_private(work / "node.env", read_bounded(node_config, LIMITS["node.env"]))
            if Path(peer_certificate).exists():
                write_private(work / "hysteria-peer.crt", read_bounded(peer_certificate, LIMITS["hysteria-peer.crt"]))
            files = {p.name: {"bytes": p.stat().st_size, "sha256": sha(p)} for p in work.iterdir()}
            manifest = {"schema": SCHEMA, "created_utc": stamp.isoformat(), "files": files,
                        "scope": "Control API SQLite, hashed token catalog, optional deployment and node configuration; no VPN backend databases."}
            write_private(work / MANIFEST, (json.dumps(manifest, indent=2) + "\n").encode())
            with pending.open("xb") as stream:
                pending.chmod(0o600)
                with tarfile.open(fileobj=stream, mode="w:gz", format=tarfile.USTAR_FORMAT) as tar:
                    for p in sorted(work.iterdir()):
                        entry = tarfile.TarInfo(p.name)
                        entry.mode, entry.size, entry.mtime = 0o600, p.stat().st_size, int(stamp.timestamp())
                        with p.open("rb") as source:
                            tar.addfile(entry, source)
                stream.flush()
                os.fsync(stream.fileno())
            if pending.stat().st_size > MAX_ARCHIVE:
                raise BackupError("archive_size_limit")
            verify(pending)
            os.rename(pending, archive)
            parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        return {"created": True, "archive": str(archive), "sha256": sha(archive),
                "bytes": archive.stat().st_size, "retention_deleted": retain(directory), "backend_accounts_included": False}
    finally:
        if pending.exists():
            pending.unlink()


class StrictMember(tarfile.TarInfo):
    def _proc_member(self, archive):
        if self.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or self.name not in LIMITS:
            raise BackupError("unexpected_archive_member")
        if self.size < 0 or self.size > LIMITS[self.name]:
            raise BackupError("member_size_limit")
        return super()._proc_member(archive)


class BoundedReader:
    def __init__(self, stream):
        self.stream, self.total, self.deadline = stream, 0, time.monotonic() + 60
    def read(self, size=-1):
        if time.monotonic() > self.deadline:
            raise BackupError("verification_deadline")
        data = self.stream.read(min(size if size >= 0 else 65536, MAX_TAR - self.total + 1))
        self.total += len(data)
        if self.total > MAX_TAR:
            raise BackupError("expanded_archive_limit")
        return data


def verify(archive, extract_to=None):
    os.umask(0o077)
    archive = Path(archive).absolute()
    info = archive.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARCHIVE:
        raise BackupError("archive_size_or_type")
    target = Path(extract_to).absolute() if extract_to is not None else None
    if target is not None and (target.exists() or target.is_symlink()):
        raise BackupError("destination_exists")
    with tempfile.TemporaryDirectory(prefix="vpn-control-verify-") as folder:
        work = Path(folder)
        work.chmod(0o700)
        seen = set()
        fd = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            with os.fdopen(fd, "rb") as source, gzip.GzipFile(fileobj=source) as zipped:
                reader = BoundedReader(zipped)
                with tarfile.open(fileobj=reader, mode="r|", tarinfo=StrictMember) as tar:
                    for entry in tar:
                        if entry.name in seen:
                            raise BackupError("duplicate_archive_member")
                        seen.add(entry.name)
                        content = tar.extractfile(entry)
                        data = content.read(LIMITS[entry.name] + 1)
                        if len(data) != entry.size:
                            raise BackupError("member_size_mismatch")
                        write_private(work / entry.name, data)
                # Check the gzip trailer/CRC too, including trailing padding.
                # The same expansion bound remains in force after tar EOF.
                while reader.read(65536):
                    pass
        except (tarfile.TarError, EOFError, OSError) as exc:
            raise BackupError("invalid_archive") from None
        if not {MANIFEST, "control.sqlite3", "tokens.json"}.issubset(seen):
            raise BackupError("required_archive_member_missing")
        try:
            manifest = json.loads((work / MANIFEST).read_bytes())
            if manifest["schema"] != SCHEMA or set(manifest["files"]) != seen - {MANIFEST}:
                raise BackupError("invalid_manifest")
            for name, expected in manifest["files"].items():
                p = work / name
                if p.stat().st_size != expected["bytes"] or sha(p) != expected["sha256"]:
                    raise BackupError("member_hash_mismatch")
        except (KeyError, ValueError, TypeError):
            raise BackupError("invalid_manifest") from None
        validate_tokens((work / "tokens.json").read_bytes())
        check_database(work / "control.sqlite3")
        if target is not None:
            private_directory(target, new=True)
            try:
                for p in work.iterdir():
                    write_private(target / p.name, p.read_bytes())
            except BaseException:
                shutil.rmtree(target)
                raise
        return {"verified": True, "archive": str(archive), "sha256": sha(archive),
                "members": sorted(seen), "sqlite_quick_check": "ok", "extract_to": str(target) if target else None,
                "backend_accounts_included": False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest="action", required=True)
    c = commands.add_parser("create")
    c.add_argument("--database", type=Path, default=Path("/var/lib/vpn-control/control.sqlite3"))
    c.add_argument("--tokens", type=Path, default=Path("/etc/vpn-control/tokens.json"))
    c.add_argument("--deployment", type=Path, default=Path("/opt/vpn-control/deploy.json"))
    c.add_argument("--directory", type=Path, default=Path("/var/backups/vpn-control"))
    c.add_argument("--node-config", type=Path, default=Path("/etc/vpn-control/node.env"))
    c.add_argument("--peer-certificate", type=Path, default=Path("/etc/vpn-control/hysteria-peer.crt"))
    v = commands.add_parser("verify")
    v.add_argument("archive", type=Path)
    v.add_argument("--extract-to", type=Path)
    args = p.parse_args()
    try:
        result = create(args.database, args.tokens, args.deployment, args.directory, args.node_config, args.peer_certificate) if args.action == "create" else verify(args.archive, args.extract_to)
        print(json.dumps(result))
    except (BackupError, OSError, sqlite3.Error) as exc:
        code = str(exc) if isinstance(exc, BackupError) else "backup_operation_failed"
        print(json.dumps({"error": code}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
