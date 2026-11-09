#!/usr/bin/env python3
"""Build a deterministic source-only runtime artifact; never read credentials."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile


ROOT = Path(__file__).resolve().parent


def build(root=ROOT):
    root = Path(root)
    paths = [root / name for name in ("README.md", "requirements.txt", "requirements.lock")]
    package = root / "hysteria_runtime"
    paths.extend(p for p in package.rglob("*.py") if not set(p.relative_to(root).parts) & {"private", "tests", "__pycache__", ".git"})
    if not package.is_dir() or not paths:
        raise ValueError("source_package_missing")
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for path in sorted(paths, key=lambda p: p.relative_to(root).as_posix()):
                if path.is_symlink() or not path.is_file():
                    raise ValueError("source_file_invalid")
                data = path.read_bytes()
                member = tarfile.TarInfo("app/" + path.relative_to(root).as_posix())
                member.size, member.mode, member.mtime = len(data), 0o644, 0
                member.uid = member.gid = 0
                member.uname = member.gname = "root"
                archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


def provenance(root=ROOT):
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, stderr=subprocess.DEVNULL, text=True).strip()
        status = subprocess.check_output(["git", "status", "--porcelain", "--", "."], cwd=root,
                                         stderr=subprocess.DEVNULL, text=True).strip()
        return {"source_commit": commit, "source_tree_dirty": bool(status)}
    except (OSError, subprocess.CalledProcessError):
        return {"source_commit": None, "source_tree_dirty": None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    bundle = build()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    name = "hysteria-runtime-0.1.0.tar.gz"
    (args.output_dir / name).write_bytes(bundle)
    manifest = {"schema": "hysteria-runtime-artifact/v1", "version": "0.1.0", "artifact": name,
                "sha256": hashlib.sha256(bundle).hexdigest(), "size_bytes": len(bundle),
                **provenance(), "scope": "new-node/staging; production legacy migration requires separate rollout"}
    (args.output_dir / "hysteria-runtime-release.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
