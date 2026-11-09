"""Release artifacts contain only verified component image pins and source SHA."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = {
    "VPN_ADMIN_IMAGE": "vpn-admin",
    "VPN_BOT_IMAGE": "vpn-bot",
    "VPN_BILLING_IMAGE": "vpn-billing",
    "VPN_MIGRATIONS_IMAGE": "vpn-migrations",
    "VPN_ADMIN_FRONTEND_IMAGE": "vpn-admin-frontend",
    "VPN_NGINX_IMAGE": "vpn-nginx",
}


@pytest.mark.parametrize(
    "case", ["valid", "missing", "invalid_digest", "wrong_component"]
)
def test_build_only_release_manifest(tmp_path, case):
    workflow = (ROOT / ".github/workflows/publish-platform.yml").read_text()
    code = workflow.split("          python3 - <<'PY'\n", 1)[1]
    code = dedent(code.split("\n          PY", 1)[0])
    fragments = tmp_path / "release-fragments"
    fragments.mkdir()
    for index, (key, image) in enumerate(COMPONENTS.items()):
        if case == "missing" and index == 0:
            continue
        if case == "wrong_component" and index == 0:
            image = "vpn-bot"
        digest = (
            "x" * 64
            if case == "invalid_digest" and index == 0
            else format(index, "064x")
        )
        value = f"docker.io/test-user/{image}@sha256:{digest}"
        (fragments / f"{key}.env").write_text(f"{key}={value}\n")

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={
            **os.environ,
            "RELEASE_SHA": "a" * 40,
            "SOURCE_REPOSITORY": "example/VPN",
            "BUILD_RUN_ID": "123",
            "BUILD_RUN_ATTEMPT": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) == (case == "valid"), result.stderr
    if case == "valid":
        manifest = json.loads((tmp_path / "platform-release/images.json").read_text())
        assert manifest["source_commit"] == "a" * 40
        assert manifest["deployment_performed"] is False
        assert set(manifest["images"]) == set(COMPONENTS)
        rows = (tmp_path / "platform-release/release.env").read_text().splitlines()
        assert {row.split("=", 1)[0] for row in rows} == set(COMPONENTS) | {
            "RELEASE_SHA"
        }
