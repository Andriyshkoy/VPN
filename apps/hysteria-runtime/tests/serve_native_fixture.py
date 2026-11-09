"""Disposable local MongoDB + genuine runtime HTTP fixture for native QUIC tests.

The passwords below are public test data. This helper never accepts an existing
database name, binds only to loopback and drops only its randomly named database.
"""
import argparse
import contextlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sys
from urllib.parse import parse_qsl, urlsplit


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hysteria_runtime.runtime import BoundedServer, engine_caps, make_handler


PASSWORD = "fixture-password"
USERS = {"limited": "native_limited", "unlimited": "native_unlimited", "other": "native_other"}


def local_mongo_uri(value):
    """Reject nonlocal seeds, SRV, peer discovery and proxy URI options."""
    try:
        if not isinstance(value, str) or any(character.isspace() for character in value):
            raise ValueError
        parsed = urlsplit(value)
        host = parsed.hostname
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback
        if (parsed.scheme != "mongodb" or not local or "," in parsed.netloc or parsed.fragment
                or parsed.path not in ("", "/")
                or parsed.port is not None and not 1 <= parsed.port <= 65535
                or (parsed.username is None) != (parsed.password is None)
                or parsed.username == "" or parsed.password == ""):
            raise ValueError
        options = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        if len(dict(options)) != len(options):
            raise ValueError
        for name, item in options:
            if name == "authSource" and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,62}", item):
                continue
            if name == "authMechanism" and item == "SCRAM-SHA-256":
                continue
            if name == "directConnection" and item == "true":
                continue
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("fixture_requires_loopback_mongo_uri") from None
    return value


def write_ready(path, metadata):
    """Atomically publish bounded fixture metadata with private file mode."""
    target = Path(path)
    temporary = target.with_name(target.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(metadata, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def interrupted(_signal, _frame):
    # serve_forever runs on this thread. Calling shutdown here would deadlock.
    raise KeyboardInterrupt


def native_engine_supported():
    """Read the native harness's current statistics endpoint on every auth."""
    path = os.environ.get("HYS_NATIVE_ENGINE_READY")
    if not path:
        return False
    try:
        with open(path, "rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            return False
        config = json.loads(raw)
        if not isinstance(config, dict) or set(config) != {"stats_url", "stats_secret"}:
            return False
        return engine_caps(config) is True
    except (OSError, ValueError, TypeError):
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ready-file", required=True, type=Path)
    parser.add_argument("--port", default=0, type=int, help="Loopback HTTP port; 0 chooses an ephemeral port")
    arguments = parser.parse_args(argv)
    if not 0 <= arguments.port <= 65535:
        parser.error("port must be between 0 and 65535")
    try:
        uri = local_mongo_uri(os.environ.get("HYS_NATIVE_MONGO_URI", "mongodb://127.0.0.1:27017/"))
    except ValueError:
        parser.error("HYS_NATIVE_MONGO_URI must be a direct loopback MongoDB URI")

    from bson.int64 import Int64
    from pymongo import MongoClient

    database_name = "vpn_hys_native_" + str(os.getpid()) + "_" + secrets.token_hex(8)
    client = None
    server = None
    seeded = False
    exit_status = 0
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        client = MongoClient(uri, directConnection=True, serverSelectionTimeoutMS=3000,
                             connectTimeoutMS=3000, socketTimeoutMS=3000)
        database = client[database_name]
        database.command("ping")
        rows = [{"_id": name, "password": PASSWORD,
                 "max_sessions": None if label == "unlimited" else Int64(1),
                 "max_download_bytes": Int64(10 * 1024**3),
                 "upload_bytes": Int64(0), "download_bytes": Int64(0),
                 "expiration_days": 0, "unlimited_user": False, "blocked": False}
                for label, name in USERS.items()]
        # Mark ownership before inserting so even an uncertain acknowledgement
        # is followed by cleanup of this helper's uniquely named database.
        seeded = True
        database.users.insert_many(rows, ordered=True)
        server = BoundedServer(("127.0.0.1", arguments.port),
                               make_handler(database.users, lambda: database.command("ping"),
                                            engine_supported=native_engine_supported))
        metadata = {"auth_url": "http://127.0.0.1:" + str(server.server_port) + "/auth",
                    "users": {label: {"auth": name + ":" + PASSWORD, "id": name}
                              for label, name in USERS.items()},
                    "database": database_name}
        write_ready(arguments.ready_file, metadata)
        print(json.dumps(metadata, sort_keys=True), flush=True)
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    except Exception:
        # URI credentials and Mongo exception text must never reach test logs.
        print("native runtime fixture failed", file=sys.stderr)
        exit_status = 1
    finally:
        if server is not None:
            server.server_close()
        if seeded:
            try:
                client.drop_database(database_name)
            except Exception:
                print("native runtime fixture cleanup failed", file=sys.stderr)
                exit_status = 1
        if client is not None:
            client.close()
    return exit_status


if __name__ == "__main__":
    raise SystemExit(main())
