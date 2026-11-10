"""Verify the production bind and execute the actual pre-migration probe."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from core.exceptions import APIConfigurationError

ROOT = Path(__file__).resolve().parents[1]
HOST = "/etc/vpn-hub/node-agent-endpoints.json"
TARGET = "/run/secrets/node-agent-endpoints.json"
TOKEN = "catalog-test-only-" + "x" * 40
IMAGE = "example.invalid/vpn-bot@sha256:" + "b" * 64


def _block(compose: str, service: str) -> str:
    match = re.search(
        rf"^  {re.escape(service)}:\n(?P<body>(?:^(?:    |$).*\n?)*)",
        compose,
        re.MULTILINE,
    )
    assert match is not None, service
    return match.group(0)


def test_catalog_is_a_readonly_existing_file_mount_on_only_four_backends():
    compose = (ROOT / "docker-compose-prod.yml").read_text()
    mount = re.search(
        r"^x-vpn-node-catalog-volume: &(?P<alias>[^\n]+)\n" r"(?P<body>(?:^  .*\n?)*)",
        compose,
        re.MULTILINE,
    )
    assert mount is not None
    body, alias = mount.group("body"), mount.group("alias")
    assert "type: bind" in body and f"source: {HOST}\n" in body
    assert f"target: {TARGET}\n" in body and "read_only: true" in body
    assert "bind:\n    create_host_path: false" in body
    policy = re.search(
        r"^x-release-policy-environment: &release-policy-environment\n"
        r"(?P<body>(?:^  .*\n?)*)",
        compose,
        re.MULTILINE,
    )
    assert policy is not None
    assert f"NODE_AGENT_ENDPOINTS_FILE: {TARGET}\n" in policy.group("body")
    for service in ("admin", "bot", "rq_worker", "rq_scheduler"):
        service_body = _block(compose, service)
        assert f"- *{alias}" in service_body, service
        assert "*release-policy-environment" in service_body, service
        assert "NODE_AGENT_ENDPOINTS_FILE:" not in service_body
    for service in ("db", "redis", "migrations", "admin_frontend", "nginx"):
        service_body = _block(compose, service)
        assert f"*{alias}" not in service_body
        assert "*release-policy-environment" not in service_body
        assert "NODE_AGENT_ENDPOINTS_FILE" not in service_body


def _function_source():
    script = (ROOT / "deploy/production_canary.sh").read_text()
    match = re.search(r"^verify_node_catalog\(\) \{\n.*?^\}", script, re.M | re.S)
    assert match is not None
    function = match.group(0)
    assert f"local catalog={HOST}" in function
    return function


def _catalog(tmp_path, *, mode=0o600):
    file = tmp_path / "node-catalog.json"
    file.write_text(
        json.dumps(
            {
                "nl-1": {
                    "base_url": "http://10.77.78.2:18010",
                    "token": TOKEN,
                    "transport": "wireguard-http",
                }
            }
        )
    )
    file.chmod(mode)
    return file


def _capture_probe(tmp_path, catalog=None, *, docker_status=0):
    catalog = catalog or _catalog(tmp_path)
    function = _function_source().replace(
        f"local catalog={HOST}", "local catalog=" + shlex.quote(str(catalog)), 1
    )
    captured = tmp_path / "docker-arguments.json"
    mock = (
        'docker() { "$TEST_PYTHON" -c '
        + shlex.quote(
            "import json,os,sys; "
            "open(os.environ['CAPTURE_FILE'],'w').write(json.dumps(sys.argv[1:]))"
        )
        + ' "$@"; return "$FAKE_DOCKER_STATUS"; }\n'
    )
    result = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-euc",
            function + "\n" + mock + 'verify_node_catalog "$SELECTED_IMAGE"',
        ],
        env={
            **os.environ,
            "TEST_PYTHON": sys.executable,
            "CAPTURE_FILE": str(captured),
            "FAKE_DOCKER_STATUS": str(docker_status),
            "SELECTED_IMAGE": IMAGE,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result, json.loads(captured.read_text()) if captured.exists() else None


def test_probe_uses_selected_default_application_uid_and_has_no_mutation_network(
    tmp_path,
):
    result, command = _capture_probe(tmp_path)
    assert result.returncode == 0
    assert command[:2] == ["run", "--rm"]
    assert command[command.index("--network") + 1] == "none"
    assert "--read-only" in command and "--cap-drop" in command
    assert "no-new-privileges:true" in command
    assert command[command.index("--entrypoint") + 1] == "python"
    assert command[-3] == IMAGE and command[-2] == "-c"
    assert "--user" not in command and "--env-file" not in command
    bind = command[command.index("--mount") + 1]
    assert "type=bind" in bind and f"target={TARGET}" in bind
    assert bind.endswith(",readonly") and "create_host_path" not in bind
    for flag in (
        "MAINTENANCE_MODE=true",
        "BILLING_ENABLED=false",
        "PAYMENTS_ENABLED=false",
        "PROVISIONING_ENABLED=false",
        "NOTIFICATIONS_ENABLED=false",
        f"NODE_AGENT_ENDPOINTS_FILE={TARGET}",
    ):
        assert flag in command
    assert "bot.main" not in command[-1]
    assert TOKEN not in result.stdout + result.stderr


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink"])
def test_host_missing_nonfile_or_symlink_stops_before_docker(tmp_path, kind):
    if kind == "missing":
        catalog = tmp_path / "missing.json"
    elif kind == "directory":
        catalog = tmp_path / "directory"
        catalog.mkdir()
    else:
        real = _catalog(tmp_path)
        catalog = tmp_path / "link.json"
        catalog.symlink_to(real)
    result, command = _capture_probe(tmp_path, catalog)
    assert result.returncode != 0 and command is None


def _execute_actual_probe(code, catalog, monkeypatch, *, uid=10001, env_path=TARGET):
    # Model the real app UID/file owner while retaining actual file reading,
    # no-follow flags, permissions, JSON validation and canonical origin checks.
    original_open, original_stat = os.open, os.fstat
    reads = []

    def open_catalog(path, flags, *args, **kwargs):
        if str(path) == TARGET:
            reads.append(flags)
            return original_open(catalog, flags, *args, **kwargs)
        return original_open(path, flags, *args, **kwargs)

    def file_stat(descriptor):
        values = list(original_stat(descriptor))
        values[4] = 10001
        return os.stat_result(values)

    with monkeypatch.context() as patch:
        patch.setenv("NODE_AGENT_ENDPOINTS_FILE", env_path)
        patch.setattr(os, "geteuid", lambda: uid)
        patch.setattr(os, "open", open_catalog)
        patch.setattr(os, "fstat", file_stat)
        exec(compile(code, "<actual-canary-catalog-probe>", "exec"), {})
    return reads


def test_actual_python_probe_reads_private_catalog_without_printing_registration(
    tmp_path, monkeypatch, capsys
):
    _, command = _capture_probe(tmp_path)
    reads = _execute_actual_probe(command[-1], _catalog(tmp_path), monkeypatch)
    assert reads and reads[0] & os.O_NOFOLLOW
    output = capsys.readouterr()
    assert output.out.strip() == "node_endpoint_catalog_ok"
    assert TOKEN not in output.out + output.err
    assert "10.77.78.2" not in output.out + output.err


@pytest.mark.parametrize("uid", [0, 1000, 101])
def test_actual_python_probe_rejects_root_or_an_unexpected_image_user(
    tmp_path, monkeypatch, uid
):
    _, command = _capture_probe(tmp_path)
    with pytest.raises(AssertionError, match="application_uid"):
        _execute_actual_probe(command[-1], _catalog(tmp_path), monkeypatch, uid=uid)


@pytest.mark.parametrize("kind", ["missing", "invalid_json", "public_mode", "symlink"])
def test_actual_python_probe_rejects_unusable_catalog(tmp_path, monkeypatch, kind):
    _, command = _capture_probe(tmp_path)
    catalog = _catalog(tmp_path)
    if kind == "missing":
        catalog.unlink()
    elif kind == "invalid_json":
        catalog.write_text("not a catalog")
    elif kind == "public_mode":
        catalog.chmod(0o644)
    else:
        link = tmp_path / "inside-container-link.json"
        link.symlink_to(catalog)
        catalog = link
    with pytest.raises(APIConfigurationError):
        _execute_actual_probe(command[-1], catalog, monkeypatch)


def test_probe_failure_aborts_actual_canary_gate_before_live_migration(tmp_path):
    script = (ROOT / "deploy/production_canary.sh").read_text()
    start = script.index('log "verifying the existing node catalog')
    end = script.index("\nbootstrap_postgres_exporter_role", start)
    assert start < script.index("LIVE_MIGRATION_STARTED=true", start)
    marker = tmp_path / "migration-started"
    harness = (
        "log() { :; }; die() { exit 73; }; verify_node_catalog() { return 42; }; "
        "BACKUP_DIR="
        + shlex.quote(str(tmp_path))
        + "; BOT_IMAGE=selected-image;\n"
        + script[start:end]
        + "\n: > "
        + shlex.quote(str(marker))
    )
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-euc", harness],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 73 and not marker.exists()


def test_failed_docker_probe_is_not_reported_as_a_success(tmp_path):
    result, command = _capture_probe(tmp_path, docker_status=47)
    assert command is not None and result.returncode == 47
