"""Adapter invariants at the real operator/HTTP-client boundary, without live users."""
from copy import deepcopy
from dataclasses import asdict
import json
import time

import pytest

from vpn_control.common import BackendConflict, BackendUnavailable
from vpn_control.marzban import MarzbanBackend
from vpn_control.vendor.marzban_manage import OperatorError


NAME = "vc_" + "a" * 24
IDENTITY = "00000000-0000-4000-8000-000000000001"
TAG = "VLESS_REALITY_NL"


def link(identity=IDENTITY):
    return (f"vless://{identity}@195.133.195.40:9443?type=tcp&security=reality"
            "&flow=xtls-rprx-vision&sni=hst.andriyshkoy.ru&fp=chrome&pbk=FAKE-PUBLIC-KEY&sid=aabbccdd")


class FakeAPI:
    def __init__(self):
        self.credentials = {"inbound_tag": TAG, "password": "FAKE-OPERATOR-PASSWORD"}
        self.users = {}
        self.calls = []
        self.fail_after_post = False
        self.fail_before_put = False
        self.fail_after_put = False
        self.fail_after_delete = False

    def user(self, name):
        return deepcopy(self.users.get(name))

    def request(self, method, path, payload=None):
        self.calls.append((method, path, deepcopy(payload)))
        if method == "POST" and path == "/api/user":
            name = payload["username"]
            if name in self.users:
                raise OperatorError("api_http_error", 409)
            user = deepcopy(payload)
            user.update({"used_traffic": 0, "online_at": None,
                         "links": [link()], "proxies": {"vless": {"id": IDENTITY, "flow": "xtls-rprx-vision"}}})
            self.users[name] = user
            if self.fail_after_post:
                raise OperatorError("api_connection_failed")
            return deepcopy(user)
        name = path.rsplit("/", 1)[-1]
        if method == "PUT":
            if self.fail_before_put:
                raise OperatorError("api_connection_failed")
            self.users[name].update(deepcopy(payload))
            if self.fail_after_put:
                raise OperatorError("api_connection_failed")
            return deepcopy(self.users[name])
        if method == "DELETE":
            self.users.pop(name, None)
            if self.fail_after_delete:
                raise OperatorError("api_connection_failed")
            return None
        if path == "/api/admin":
            return {"is_sudo": True}
        if path == "/api/core":
            return {"started": True, "version": "26.3.27"}
        if path == "/api/system":
            return {"online_users": 0}
        raise AssertionError("unexpected fake request")


@pytest.fixture
def connected():
    api = FakeAPI()
    adapter = MarzbanBackend("/unused/private.json", api_factory=lambda: api)
    return adapter, api


def issue(adapter):
    return adapter.create(NAME, expires_at=None, traffic_limit_bytes=0)


def test_ambiguous_create_reconciles_without_rotating_identity(connected):
    adapter, api = connected
    api.fail_after_post = True
    assert issue(adapter).status == "active"
    assert issue(adapter).status == "active"
    assert len([c for c in api.calls if c[0] == "POST"]) == 1
    assert api.users[NAME]["proxies"]["vless"]["id"] == IDENTITY
    assert api.users[NAME]["note"] == "vpn-control:" + NAME


def test_foreign_record_is_not_adopted_read_modified_or_deleted(connected):
    adapter, api = connected
    issue(adapter)
    api.users[NAME]["note"] = "legacy account"
    calls = len(api.calls)
    for action in (
        lambda: adapter.get(NAME), lambda: issue(adapter),
        lambda: adapter.update(NAME, expires_at=None, traffic_limit_bytes=7),
        lambda: adapter.set_status(NAME, "suspended"),
        lambda: adapter.profile(NAME), lambda: adapter.revoke(NAME),
    ):
        with pytest.raises(BackendConflict, match="backend_ownership_conflict"):
            action()
    assert len(api.calls) == calls
    assert NAME in api.users


def test_legacy_username_rejected_before_backend_io(connected):
    adapter, api = connected
    with pytest.raises(BackendConflict, match="backend_reference_invalid"):
        adapter.revoke("pilot_20261107_01")
    assert api.calls == []


def test_limits_update_never_sends_proxy_or_resets_traffic(connected):
    adapter, api = connected
    issue(adapter)
    api.users[NAME]["used_traffic"] = 12345
    expiry = int(time.time()) + 3600
    updated = adapter.update(NAME, expires_at=expiry, traffic_limit_bytes=10**6)
    assert updated.expires_at == expiry and updated.used_traffic_bytes == 12345
    assert api.users[NAME]["proxies"]["vless"]["id"] == IDENTITY
    put = [c[2] for c in api.calls if c[0] == "PUT"]
    assert put == [{"expire": expiry, "data_limit": 10**6, "data_limit_reset_strategy": "no_reset"}]


def test_ambiguous_update_after_commit_is_read_back(connected):
    adapter, api = connected
    issue(adapter)
    api.fail_after_put = True
    assert adapter.update(NAME, expires_at=None, traffic_limit_bytes=900).traffic_limit_bytes == 900
    assert adapter.update(NAME, expires_at=None, traffic_limit_bytes=900).traffic_limit_bytes == 900
    assert len([c for c in api.calls if c[0] == "PUT"]) == 1


def test_update_timeout_before_commit_is_retriable_unavailable(connected):
    adapter, api = connected
    issue(adapter)
    api.fail_before_put = True
    with pytest.raises(BackendUnavailable, match="backend_mutation_outcome_unknown"):
        adapter.update(NAME, expires_at=None, traffic_limit_bytes=900)
    assert api.users[NAME]["data_limit"] == 0


def test_suspend_resume_and_quota_refusal_preserve_identity(connected):
    adapter, api = connected
    issue(adapter)
    assert adapter.set_status(NAME, "suspended").status == "suspended"
    assert adapter.set_status(NAME, "active").status == "active"
    api.users[NAME].update(data_limit=100, used_traffic=100, status="disabled")
    with pytest.raises(BackendConflict):
        adapter.set_status(NAME, "active")
    assert api.users[NAME]["proxies"]["vless"]["id"] == IDENTITY


def test_backend_active_with_exhausted_limits_is_not_reported_usable(connected):
    adapter, api = connected
    issue(adapter)
    api.users[NAME].update(data_limit=10, used_traffic=10)
    assert adapter.get(NAME).status == "limited"
    api.users[NAME].update(expire=int(time.time()) - 1)
    assert adapter.get(NAME).status == "expired"


def test_profile_only_returns_unique_checked_endpoint(connected):
    adapter, api = connected
    issue(adapter)
    assert adapter.profile(NAME) == link()
    for broken in (
        [link(), link()], [link().replace(":9443?", ":8443?")],
        [link().replace("hst.andriyshkoy.ru", "attacker.invalid")],
        [link().replace("xtls-rprx-vision", "none")],
        [link().replace("00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002")],
    ):
        api.users[NAME]["links"] = broken
        with pytest.raises(BackendUnavailable, match="invalid_backend_profile"):
            adapter.profile(NAME)


def test_profile_uses_configured_node_endpoint_and_sni(connected):
    _, api = connected
    adapter = MarzbanBackend("/unused/private.json", public_host="nl2.example.net",
                             public_port=14443, reality_sni="cover.example.net",
                             api_factory=lambda: api)
    issue(adapter)
    configured = (link().replace("195.133.195.40:9443", "nl2.example.net:14443")
                  .replace("hst.andriyshkoy.ru", "cover.example.net"))
    api.users[NAME]["links"] = [configured]
    assert adapter.profile(NAME) == configured
    api.users[NAME]["links"] = [link()]
    with pytest.raises(BackendUnavailable, match="invalid_backend_profile"):
        adapter.profile(NAME)


@pytest.mark.parametrize("changes", [
    {"public_host": "https://nl2.example.net"},
    {"public_host": "nl2.example.net/path"},
    {"public_host": "nl2.example.net:9443"},
    {"public_host": "nl2.example.net\n"},
    {"reality_sni": "cover.example.net?secret=x"},
    {"public_port": True}, {"public_port": 0}, {"public_port": 65536},
])
def test_node_configuration_rejects_url_injection_and_invalid_ports(changes):
    with pytest.raises(ValueError):
        MarzbanBackend("/unused/private.json", **changes)


def test_non_export_snapshot_and_health_contain_no_credentials(connected):
    adapter, api = connected
    issue(adapter)
    api.users[NAME]["online_at"] = "2026-11-09T03:15:00Z"
    result = asdict(adapter.get(NAME))
    result["health"] = adapter.health()
    serialized = json.dumps(result)
    assert IDENTITY not in serialized and "vless://" not in serialized
    assert "FAKE-OPERATOR-PASSWORD" not in serialized and "FAKE-PUBLIC-KEY" not in serialized
    assert result["last_seen_at"] == 1794194100
    assert result["health"]["data_plane_verified"] is False


def test_revoke_reconciles_ambiguous_delete_and_is_idempotent(connected):
    adapter, api = connected
    issue(adapter)
    api.fail_after_delete = True
    assert adapter.revoke(NAME) is None
    assert adapter.revoke(NAME) is None
    assert len([c for c in api.calls if c[0] == "DELETE"]) == 1


def test_activation_days_not_silently_ignored(connected):
    adapter, api = connected
    with pytest.raises(BackendConflict, match="activation_days_unsupported"):
        adapter.create(NAME, expires_at=None, traffic_limit_bytes=0, activation_days=7)
    assert api.calls == []


def test_session_limit_not_silently_ignored(connected):
    adapter, api = connected
    for method in (adapter.create, adapter.update):
        with pytest.raises(BackendConflict, match="max_sessions_unsupported"):
            method(NAME, expires_at=None, traffic_limit_bytes=0, max_sessions=1)
    assert api.calls == []


def test_raw_upstream_errors_are_not_exposed(connected):
    adapter, api = connected
    def failed(_):
        raise OperatorError("api_connection_failed")
    api.user = failed
    with pytest.raises(BackendUnavailable) as error:
        adapter.get(NAME)
    assert str(error.value) == "marzban_unavailable"


def credentials_file(tmp_path, **changes):
    folder = tmp_path / "credentials"
    folder.mkdir(mode=0o700)
    source = {"api_url": "http://127.0.0.1:18000/", "username": "operator",
              "password": "FAKE-OPERATOR-PASSWORD", "inbound_tag": TAG}
    source.update(changes)
    path = folder / "marzban.json"
    path.write_text(json.dumps(source))
    path.chmod(0o400)
    return path


def test_systemd_readonly_credentials_are_usable_without_relaxing_modes(tmp_path):
    path = credentials_file(tmp_path)
    path.parent.chmod(0o500)
    try:
        api = MarzbanBackend(path)._new_api()
        assert api.credentials["api_url"] == "http://127.0.0.1:18000"
        assert api.credentials["password"] == "FAKE-OPERATOR-PASSWORD"
        assert path.stat().st_mode & 0o777 == 0o400
        assert path.parent.stat().st_mode & 0o777 == 0o500
    finally:
        path.parent.chmod(0o700)


@pytest.mark.parametrize("mode", [0o444, 0o640, 0o644, 0o660, 0o666])
def test_credentials_reject_permissions_readable_by_other_principals(tmp_path, mode):
    path = credentials_file(tmp_path)
    path.chmod(mode)
    with pytest.raises(BackendUnavailable) as error:
        MarzbanBackend(path)._new_api()
    assert str(error.value) == "marzban_credentials_unavailable"
    assert "FAKE-OPERATOR-PASSWORD" not in str(error.value)


def test_credentials_reject_symlink_leaf(tmp_path):
    path = credentials_file(tmp_path)
    link_path = path.parent / "link.json"
    link_path.symlink_to(path.name)
    with pytest.raises(BackendUnavailable, match="marzban_credentials_unavailable"):
        MarzbanBackend(link_path)._new_api()


@pytest.mark.parametrize("changes", [
    {"api_url": "http://192.0.2.1:18000"},
    {"api_url": "http://localhost:18000"},
    {"api_url": "https://127.0.0.1:18000"},
    {"api_url": "http://username:secret@127.0.0.1:18000"},
    {"api_url": "http://127.0.0.1"},
    {"api_url": "http://127.0.0.1:70000"},
    {"api_url": "http://127.0.0.1:18000/api"},
    {"api_url": "http://127.0.0.1:18000/?query=secret"},
    {"api_url": "http://127.0.0.1:18000/#fragment"},
    {"api_url": "http://127.0.0.1:18000\n"},
    {"username": ""},
    {"password": None},
    {"inbound_tag": "OTHER_SERVICE"},
])
def test_credentials_retain_loopback_target_and_required_field_validation(tmp_path, changes):
    path = credentials_file(tmp_path, **changes)
    with pytest.raises(BackendUnavailable, match="marzban_credentials_unavailable"):
        MarzbanBackend(path)._new_api()
