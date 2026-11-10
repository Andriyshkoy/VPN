"""Exercise the real workflow gate against synthetic GitHub API responses."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
GATE_STEPS = (
    ("release.yml", "Require successful CI for the exact release SHA"),
    ("release.yml", "Reconfirm current main and exact successful CI after approval"),
    ("publish-platform.yml", "Require successful CI for the exact main SHA"),
)


def _step_script(workflow, name):
    block = workflow.split(f"      - name: {name}\n", 1)[1]
    lines = block.split("        run: |\n", 1)[1].splitlines()
    script = []
    for line in lines:
        if line.startswith("          "):
            script.append(line[10:])
        elif not line.strip():
            script.append("")
        else:
            break
    return "\n".join(script)


def _run_gate(tmp_path, workflow_name, step, runs, *, current_main=SHA):
    fixture_bin = tmp_path / "bin"
    fixture_bin.mkdir()
    # The fake command cannot forward an unrecognized request to a real API.
    gh = fixture_bin / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "assert sys.argv[1] == 'api'\n"
        "endpoint = sys.argv[2]\n"
        "with pathlib.Path(os.environ['GH_REQUEST_LOG']).open('a') as log:\n"
        "    log.write(endpoint + '\\n')\n"
        "if endpoint.endswith('/commits/main'):\n"
        "    print(os.environ['GH_FIXTURE_MAIN'])\n"
        "elif '/actions/workflows/ci.yml/runs?' in endpoint:\n"
        "    print(json.dumps({'workflow_runs': [{'head_sha': os.environ['RELEASE_SHA'], 'status': 'completed', 'conclusion': 'success'}]}))\n"
        "elif '/actions/workflows/hysteria-session-limit.yml/runs?' in endpoint:\n"
        "    print(os.environ['GH_FIXTURE_NATIVE'])\n"
        "else:\n"
        "    raise SystemExit('Unexpected synthetic API request')\n"
    )
    gh.chmod(0o700)
    workflow = (ROOT / ".github/workflows" / workflow_name).read_text()
    result = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-eo",
            "pipefail",
            "-c",
            _step_script(workflow, step),
        ],
        env={
            **os.environ,
            "PATH": str(fixture_bin) + os.pathsep + os.environ["PATH"],
            "RELEASE_SHA": SHA,
            "REPOSITORY": "fixture/project",
            "GH_TOKEN": "synthetic-unused-token",
            "GH_FIXTURE_MAIN": current_main,
            "GH_FIXTURE_NATIVE": json.dumps({"workflow_runs": runs}),
            "GH_REQUEST_LOG": str(tmp_path / "requests.log"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result, (tmp_path / "requests.log").read_text().splitlines()


@pytest.mark.parametrize("workflow_name, step", GATE_STEPS)
@pytest.mark.parametrize(
    "case",
    [
        "success",
        "missing",
        "stale_sha",
        "other_branch",
        "pull_request",
        "manual_dispatch",
        "in_progress",
        "failed",
        "cancelled_after_success",
    ],
)
def test_native_acceptance_is_required_before_build_and_after_approval(
    tmp_path, workflow_name, step, case
):
    run = {
        "head_sha": SHA,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
    }
    runs = [run]
    if case == "missing":
        runs = []
    elif case == "stale_sha":
        run["head_sha"] = "b" * 40
    elif case == "other_branch":
        run["head_branch"] = "candidate"
    elif case in {"pull_request", "manual_dispatch"}:
        run["event"] = "pull_request" if case == "pull_request" else "workflow_dispatch"
    elif case == "in_progress":
        run.update(status="in_progress", conclusion=None)
    elif case == "failed":
        run["conclusion"] = "failure"
    elif case == "cancelled_after_success":
        runs = [{**run, "conclusion": "cancelled"}, run]
    result, requests = _run_gate(tmp_path, workflow_name, step, runs)
    assert (result.returncode == 0) == (case == "success"), result.stderr
    assert requests[-1] == (
        "repos/fixture/project/actions/workflows/hysteria-session-limit.yml/runs"
        f"?head_sha={SHA}&event=push&per_page=20"
    )


@pytest.mark.parametrize("workflow_name, step", GATE_STEPS)
def test_native_success_cannot_release_an_outdated_main(tmp_path, workflow_name, step):
    result, requests = _run_gate(
        tmp_path, workflow_name, step, [], current_main="b" * 40
    )
    assert result.returncode != 0
    assert requests == ["repos/fixture/project/commits/main"]


def test_native_workflow_covers_every_main_commit_without_publication_or_deploy():
    workflow = (ROOT / ".github/workflows/hysteria-session-limit.yml").read_text()
    trigger = workflow.split("\non:\n", 1)[1].split("\npermissions:\n", 1)[0]
    push = trigger.split("  push:\n", 1)[1].split("  workflow_dispatch:", 1)[0]
    assert "branches: [main]" in push and "paths:" not in push
    assert "paths:" in trigger.split("  push:\n", 1)[0]
    assert "workflow_dispatch:" in trigger
    assert "actions/upload-artifact@" in workflow
    assert not any(
        mutation in workflow
        for mutation in ("environment: production", "push: true", "ssh ", "scp ")
    )


@pytest.mark.parametrize("case", ["pass", "skipped", "failed", "no_tests", "one_test"])
def test_native_stream_enforcement_step_cannot_succeed_without_running_tests(
    tmp_path, case
):
    workflow = (ROOT / ".github/workflows/hysteria-session-limit.yml").read_text()
    step = "Require quota and expiry to close real native streams"
    assert workflow.index("Build exact engine") < workflow.index(step)
    assert workflow.index(step) < workflow.index("Upload prepared engine")
    test_dir = tmp_path / "apps/hysteria-runtime/tests"
    test_dir.mkdir(parents=True)
    body = "import unittest\n"
    if case != "no_tests":
        body += "class NativePolicyEnforcementTests(unittest.TestCase):\n"
        if case == "skipped":
            body += "    @unittest.skip('missing mandatory native fixture')\n"
        body += "    def test_stream_policy(self):\n"
        body += (
            "        self.fail('quota did not close stream')\n"
            if case == "failed"
            else "        pass\n"
        )
        if case != "one_test":
            body += "    def test_first_use_compare_and_set(self):\n        pass\n"
    (test_dir / "test_native_enforcement.py").write_text(body)
    venv_bin = tmp_path / "runner/hys-native-venv/bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(sys.executable)
    result = subprocess.run(
        [
            "bash",
            "--noprofile",
            "--norc",
            "-eo",
            "pipefail",
            "-c",
            _step_script(workflow, step),
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "RUNNER_TEMP": str(tmp_path / "runner"),
            "PYTHONPATH": "apps/hysteria-runtime:apps/hysteria-runtime/tests",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) == (case == "pass"), result.stderr
