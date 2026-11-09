"""Exercise ordinary private files and real systemd255 credential metadata."""
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from vpn_control.config import private_json


@pytest.fixture
def systemd_copy(tmp_path, monkeypatch):
    directory = tmp_path / "service.credentials"
    directory.mkdir(mode=0o700)
    path = directory / "tokens.json"
    path.write_text('{"tokens": []}')
    path.chmod(0o440)
    directory.chmod(0o550)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(directory))
    real_fstat = os.fstat

    def root_owned(fd):
        values = list(real_fstat(fd))
        values[4] = 0  # systemd files and parent remain owned by root.
        values[5] = 0
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", root_owned)
    monkeypatch.setattr(os, "geteuid", lambda: 990)
    monkeypatch.setattr(os, "fstatvfs", lambda fd: SimpleNamespace(f_flag=os.ST_RDONLY))
    yield path, directory
    directory.chmod(0o700)


def test_systemd_255_root_440_acl_credential_readable(systemd_copy):
    path, directory = systemd_copy
    assert private_json(path) == {"tokens": []}
    assert stat.S_IMODE(path.stat().st_mode) == 0o440
    assert stat.S_IMODE(directory.stat().st_mode) == 0o550


def test_systemd_copy_must_be_on_readonly_mount(systemd_copy, monkeypatch):
    path, _ = systemd_copy
    monkeypatch.setattr(os, "fstatvfs", lambda fd: SimpleNamespace(f_flag=0))
    with pytest.raises(ValueError, match="owned_and_private"):
        private_json(path)


def test_systemd_exception_does_not_accept_root_file_outside_directory(systemd_copy, monkeypatch):
    path, directory = systemd_copy
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(directory.parent))
    with pytest.raises(ValueError, match="owned_and_private"):
        private_json(path)


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o444, 0o444 | 0o200, 0o500])
def test_systemd_exception_rejects_writable_world_readable_or_executable_files(systemd_copy, mode):
    path, _ = systemd_copy
    path.chmod(mode)
    with pytest.raises(ValueError, match="owned_and_private"):
        private_json(path)


def test_systemd_exception_rejects_writable_parent(systemd_copy):
    path, directory = systemd_copy
    directory.chmod(0o750)
    with pytest.raises(ValueError, match="owned_and_private"):
        private_json(path)


def test_systemd_exception_rejects_symlink_leaf(systemd_copy):
    path, directory = systemd_copy
    directory.chmod(0o750)
    link = directory / "alias.json"
    link.symlink_to(path.name)
    directory.chmod(0o550)
    with pytest.raises(OSError):
        private_json(link)


def test_systemd_exception_requires_canonical_directory(systemd_copy, monkeypatch):
    path, directory = systemd_copy
    alias = directory.parent / "credentials-link"
    alias.symlink_to(directory.name, target_is_directory=True)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(alias))
    with pytest.raises(ValueError, match="owned_and_private"):
        private_json(alias / path.name)


def test_marzban_loader_uses_the_same_systemd_guard(systemd_copy):
    from vpn_control.marzban import MarzbanBackend
    path, _ = systemd_copy
    path.chmod(0o600)
    path.write_text(json.dumps({"api_url": "http://127.0.0.1:18000", "username": "operator", "password": "private-test-password", "inbound_tag": "VLESS_REALITY_NL"}))
    path.chmod(0o440)
    api = MarzbanBackend(path)._new_api()
    assert api.credentials["api_url"] == "http://127.0.0.1:18000"


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_normal_owned_private_files_still_work_outside_systemd(tmp_path, monkeypatch, mode):
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    path = tmp_path / "normal.json"
    path.write_text('{"value": 1}')
    path.chmod(mode)
    assert private_json(path) == {"value": 1}


def test_normal_group_readable_file_is_not_accepted(tmp_path, monkeypatch):
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    path = tmp_path / "normal.json"
    path.write_text('{"value": 1}')
    path.chmod(0o440)
    with pytest.raises(ValueError, match="owned_and_private"):
        private_json(path)
