#!/usr/bin/env python3
"""Build the native systemd release from the installer's explicit allowlist."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("node_agent_install", ROOT / "deploy/install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    package = module.bundle()
    checksum = hashlib.sha256(package).hexdigest()
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True
    ).strip()
    dirty = bool(subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=normal", "--", "apps/node-agent", "contracts/node-agent-v1"],
        cwd=REPOSITORY,
    ))
    metadata = json.loads((REPOSITORY / "releases/node-agent.json").read_text())
    archive = "vpn-node-agent-" + checksum[:20] + ".tar.gz"
    manifest = {
        **metadata,
        "source_commit": revision,
        "source_tree_dirty": dirty,
        "archive": archive,
        "archive_sha256": checksum,
        "archive_bytes": len(package),
        "runtime_lock_sha256": hashlib.sha256((ROOT / "requirements.lock").read_bytes()).hexdigest(),
        "openapi_sha256": hashlib.sha256((REPOSITORY / "contracts/node-agent-v1/openapi.json").read_bytes()).hexdigest(),
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / archive).write_bytes(package)
    (args.output_dir / "release.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"archive": archive, "sha256": checksum, "source_commit": revision, "source_tree_dirty": dirty}))


if __name__ == "__main__":
    main()
