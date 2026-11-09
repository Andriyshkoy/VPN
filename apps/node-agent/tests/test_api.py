from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from vpn_control.app import create_app
from vpn_control.common import BackendAccess, BackendConflict, BackendUnavailable
from vpn_control.config import Principal, Settings, token_hash

WRITE = "test-operator-" + "a" * 40
READ = "test-reader-" + "b" * 40
EXPORT = "test-export-" + "c" * 40
PRINCIPALS = (Principal("operator", token_hash(WRITE), frozenset({"access:read", "access:write", "profiles:read"})),
              Principal("reader", token_hash(READ), frozenset({"access:read"})),
              Principal("exporter", token_hash(EXPORT), frozenset({"profiles:read"})))


class FakeBackend:
    def __init__(self):
        self.users, self.profiles, self.create_names = {}, {}, []
        self.ambiguous_once = False
        self.blocked, self.release = None, None
        self.ready = True

    def create(self, username, *, expires_at, traffic_limit_bytes, activation_days=None):
        self.create_names.append(username)
        if username not in self.users:
            self.users[username] = BackendAccess("active", 0, traffic_limit_bytes, expires_at, activation_days=activation_days)
            self.profiles[username] = "vless://CLIENT-SECRET-" + username + "@node.invalid:9443"
        if self.blocked:
            self.blocked.set()
            assert self.release.wait(5)
        if self.ambiguous_once:
            self.ambiguous_once = False
            raise BackendUnavailable("backend_mutation_outcome_unknown")
        return self.users[username]

    def get(self, username):
        return self.users.get(username)

    def update(self, username, *, expires_at, traffic_limit_bytes, activation_days=None):
        current = self.users[username]
        self.users[username] = replace(current, traffic_limit_bytes=traffic_limit_bytes, expires_at=expires_at, activation_days=activation_days)
        return self.users[username]

    def set_status(self, username, status):
        current = self.users[username]
        if status == "active" and current.traffic_limit_bytes and current.used_traffic_bytes >= current.traffic_limit_bytes:
            raise BackendConflict("resume_blocked_by_traffic_limit")
        self.users[username] = replace(current, status=status)
        return self.users[username]

    def revoke(self, username):
        self.users.pop(username, None)
        self.profiles.pop(username, None)

    def profile(self, username):
        if self.users[username].status != "active":
            raise BackendConflict("profile_requires_active_user")
        return self.profiles[username]

    def health(self):
        return {"ready": self.ready, "data_plane_verified": False}


def headers(token=WRITE, key=None):
    result = {"Authorization": "Bearer " + token}
    if key:
        result["Idempotency-Key"] = key
    return result


@pytest.fixture
def setup(tmp_path):
    hys, vless = FakeBackend(), FakeBackend()
    settings = Settings(tmp_path / "control.sqlite3", principals=PRINCIPALS)
    app = create_app(settings, {"hysteria": hys, "vless-reality": vless})
    with TestClient(app) as client:
        yield client, settings, hys, vless


def create(client, key="create-0001", service="vless-reality", **fields):
    return client.post("/v1/accesses", headers=headers(key=key), json={"service_id": service, **fields})


def test_auth_scope_and_documentation(setup):
    client, _, _, _ = setup
    assert client.get("/healthz").status_code == 200
    assert client.get("/v1/services").status_code == 401
    assert client.get("/openapi.json").status_code == 401
    assert client.get("/openapi.json", headers=headers(READ)).status_code == 200
    assert client.get("/v1/services", headers=headers(READ)).status_code == 200
    assert client.post("/v1/accesses", headers=headers(READ, "scope-test"), json={"service_id": "hysteria"}).status_code == 403
    assert client.get("/v1/services", headers={"Authorization": "Bearer " + "x" * 64}).status_code == 401


@pytest.mark.parametrize("service", ["hysteria", "vless-reality"])
def test_lifecycle_and_secret_export(setup, service):
    client, settings, hys, vless = setup
    response = create(client, service=service, owner_ref="telegram:123", traffic_limit_bytes=1024)
    assert response.status_code == 201, response.text
    body = response.json()
    access_id = body["access"]["id"]
    assert body["access"]["status"] == "active"
    assert body["access"]["control_plane_verified"] is True
    assert body["access"]["data_plane_verified"] is False
    listing = client.get("/v1/accesses", headers=headers(READ)).json()
    assert listing["total"] == 1
    assert "CLIENT-SECRET" not in json.dumps(listing)
    assert client.get(f"/v1/accesses/{access_id}/profile", headers=headers(READ)).status_code == 403
    exported = client.get(f"/v1/accesses/{access_id}/profile", headers=headers(EXPORT))
    assert exported.status_code == 200 and "CLIENT-SECRET" in exported.json()["uri"]
    assert exported.headers["cache-control"] == "no-store"
    assert b"CLIENT-SECRET" not in settings.database.read_bytes()
    before = exported.json()["uri"]
    assert client.post(f"/v1/accesses/{access_id}/suspend", headers=headers(key="suspend-0001")).json()["access"]["status"] == "suspended"
    assert client.get(f"/v1/accesses/{access_id}/profile", headers=headers()).status_code == 409
    assert client.patch(f"/v1/accesses/{access_id}", headers=headers(key="limits-00001"), json={"traffic_limit_bytes": 2048}).json()["access"]["status"] == "suspended"
    assert client.post(f"/v1/accesses/{access_id}/resume", headers=headers(key="resume-00001")).json()["access"]["status"] == "active"
    assert client.get(f"/v1/accesses/{access_id}/profile", headers=headers()).json()["uri"] == before
    assert client.get(f"/v1/accesses/{access_id}/usage", headers=headers(READ)).json()["billing_authoritative"] is False
    assert client.delete(f"/v1/accesses/{access_id}", headers=headers(key="revoke-00001")).json()["access"]["status"] == "revoked"
    assert client.get(f"/v1/accesses/{access_id}/profile", headers=headers()).status_code == 409
    assert client.post(f"/v1/accesses/{access_id}/resume", headers=headers(key="resume-00002")).status_code == 409
    assert client.delete(f"/v1/accesses/{access_id}", headers=headers(key="revoke-00002")).status_code == 200
    assert client.get(f"/v1/accesses/{access_id}", headers=headers(READ)).json()["access"]["status"] == "revoked"


def test_idempotent_replay_and_key_reuse(setup):
    client, _, _, vless = setup
    first = create(client)
    second = create(client)
    assert second.status_code == 201 and second.json() == first.json()
    assert second.headers["idempotency-replayed"] == "true"
    assert len(vless.create_names) == 1
    assert create(client, owner_ref="changed").status_code == 409
    access_id = first.json()["access"]["id"]
    assert client.post(f"/v1/accesses/{access_id}/suspend", headers=headers(key="create-0001")).status_code == 409


def test_ambiguous_create_is_reconciled_after_restart(tmp_path):
    backend = FakeBackend(); backend.ambiguous_once = True
    settings = Settings(tmp_path / "state.sqlite3", principals=PRINCIPALS)
    def application():
        return create_app(settings, {"hysteria": FakeBackend(), "vless-reality": backend})
    with TestClient(application()) as client:
        failed = create(client, key="durable-0001")
        assert failed.status_code == 503
        pending = client.get("/v1/accesses", headers=headers()).json()["accesses"][0]
        assert pending["status"] == "provisioning" and pending["operation_pending"]
    with TestClient(application()) as client:
        recovered = create(client, key="durable-0001")
        assert recovered.status_code == 201
        assert recovered.json()["access"]["id"] == pending["id"]
        assert len(set(backend.create_names)) == 1
        assert len(backend.users) == 1


def test_concurrent_same_key_reserves_one_identity(setup):
    client, _, _, backend = setup
    backend.blocked, backend.release = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(create, client, "concurrent-0001")
        assert backend.blocked.wait(3)
        second = create(client, "concurrent-0001")
        assert second.status_code == 409 and second.json()["error"]["code"] == "operation_in_progress"
        backend.release.set()
        assert first.result(timeout=3).status_code == 201
    assert len(backend.users) == len(backend.create_names) == 1


def test_expired_time_replays_but_new_past_expiry_rejected(setup, monkeypatch):
    client, _, _, _ = setup
    future = datetime.now(timezone.utc) + timedelta(minutes=1)
    first = create(client, expires_at=future.isoformat())
    assert first.status_code == 201
    import vpn_control.app as module
    actual = module.time.time()
    monkeypatch.setattr(module.time, "time", lambda: actual + 120)
    replay = create(client, expires_at=future.isoformat())
    assert replay.status_code == 201 and replay.json() == first.json()
    assert create(client, key="new-past-0001", expires_at=future.isoformat()).status_code == 422


def test_service_semantics_and_validation_do_not_echo_input(setup):
    client, _, _, _ = setup
    expiry = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    assert create(client, service="hysteria", expires_at=expiry).status_code == 422
    assert create(client, activation_days=5).status_code == 422
    assert create(client, expires_at="2030-01-01T00:00:00").status_code == 422
    secret = "unexpected-client-secret://sensitive"
    response = create(client, owner_ref=secret, wrong_field=secret)
    assert response.status_code == 422 and secret not in response.text
    assert create(client, traffic_limit_bytes=True).status_code == 422
    assert client.get("/v1/accesses/not-a-uuid", headers=headers()).status_code == 422


def test_health_reports_backend_failure(setup):
    client, _, hys, _ = setup
    assert client.get("/readyz", headers=headers()).status_code == 200
    hys.ready = False
    result = client.get("/readyz", headers=headers())
    assert result.status_code == 503 and result.json()["ready"] is False


def test_missing_idempotency_and_large_body(setup):
    client, _, _, _ = setup
    assert client.post("/v1/accesses", headers=headers(), json={"service_id": "hysteria"}).status_code == 400
    result = client.post("/v1/accesses", headers=headers(key="large-00001"), content=b"x" * 17000)
    assert result.status_code == 413 and result.headers["cache-control"] == "no-store"
    assert client.get("/v1/accesses", headers=headers()).json()["total"] == 0


def test_cached_limits_and_owner_filters(setup):
    client, _, _, _ = setup
    one = create(client, key="one-user-001", owner_ref="customer:1").json()["access"]["id"]
    create(client, key="two-user-001", service="hysteria", owner_ref="customer:2", activation_days=7)
    assert client.get("/v1/accesses?owner_ref=customer:1", headers=headers()).json()["total"] == 1
    assert client.get("/v1/accesses?service_id=hysteria", headers=headers()).json()["total"] == 1
    assert client.patch(f"/v1/accesses/{one}", headers=headers(key="empty-00001"), json={}).status_code == 422
    assert client.patch(f"/v1/accesses/{one}", headers=headers(key="clear-owner1"), json={"owner_ref": None}).json()["access"]["owner_ref"] is None


def test_pending_ambiguous_operation_blocks_other_mutation(setup):
    client, _, _, backend = setup
    backend.ambiguous_once = True
    assert create(client, key="pending-0001").status_code == 503
    row = client.get("/v1/accesses", headers=headers()).json()["accesses"][0]
    result = client.delete(f"/v1/accesses/{row['id']}", headers=headers(key="different-op-1"))
    assert result.status_code == 409 and result.json()["error"]["code"] == "access_has_pending_operation"
    assert create(client, key="pending-0001").status_code == 201
    assert client.delete(f"/v1/accesses/{row['id']}", headers=headers(key="different-op-1")).status_code == 200


def test_region_is_configuration_and_wrong_node_cannot_be_provisioned(tmp_path):
    settings = Settings(tmp_path / "regional.sqlite3", principals=PRINCIPALS, node_id="de-2", public_host="edge.example.net", country_code="DE", vless_port=14443, reality_sni="cover.example.net", hysteria_port=2443)
    backend = FakeBackend()
    with TestClient(create_app(settings, {"hysteria": FakeBackend(), "vless-reality": backend})) as client:
        catalog = client.get("/v1/services", headers=headers()).json()["services"]
        assert catalog[0]["endpoint"]["host"] == "edge.example.net"
        assert catalog[0]["endpoint"]["port"] == 2443
        assert client.get("/v1/nodes", headers=headers()).json()["nodes"][0]["id"] == "de-2"
        assert create(client, node_id="nl-1").status_code == 422
        assert not backend.users
        created = create(client)
        assert created.status_code == 201 and created.json()["access"]["node_id"] == "de-2"


def test_openapi_contract_declares_mutation_header_and_response(setup):
    client, _, _, _ = setup
    spec = client.get("/openapi.json", headers=headers()).json()
    operation = spec["paths"]["/v1/accesses"]["post"]
    header = next(x for x in operation["parameters"] if x["name"] == "Idempotency-Key")
    assert header["required"] is True and header["in"] == "header"
    assert operation["responses"]["201"]["content"]["application/json"]["schema"]["$ref"].endswith("MutationResponse")
    assert spec["paths"]["/v1/accesses/{access_id}/profile"]["get"]["x-required-scopes"] == ["profiles:read"]
