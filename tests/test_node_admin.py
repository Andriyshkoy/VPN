from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from admin.routers import admin_v1_configs
from admin.security import (
    CSRF_COOKIE_NAME,
    AdminPermission,
    AdminPrincipal,
    get_admin_principal,
    permissions_for_role,
)
from core.db.models import (
    AdminAuditEvent,
    AdminRole,
    AdminUser,
    Server,
    User,
    VPN_Config,
)
from core.exceptions import APITransportError
from core.services.node_gateway import NodeProfile

SECRET_URI = "vless://never-copy-this-secret@vpn.test:9443?security=reality"
REMOTE_ID = "a7f5dc2c-6887-475a-a75f-e5a2a0eb4c3a"
CSRF = "node-admin-test-csrf"


async def _app(sessionmaker, role):
    async with sessionmaker() as session, session.begin():
        actor = AdminUser(
            username=f"node-admin-{role.value}", password_hash="unused", role=role.value
        )
        session.add(actor)
        await session.flush()
    principal = AdminPrincipal(
        user_id=actor.id,
        username=actor.username,
        role=role,
        session_id=1,
        csrf_token_hash=hashlib.sha256(CSRF.encode()).hexdigest(),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    app = FastAPI()
    app.include_router(admin_v1_configs.router)
    app.dependency_overrides[get_admin_principal] = lambda: principal
    return app


def _client(app):
    client = AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://admin.test",
        headers={"Origin": "https://admin.test", "X-CSRF-Token": CSRF},
    )
    client.cookies.set(CSRF_COOKIE_NAME, CSRF, path="/")
    return client


def _request():
    return {
        "server_id": 1,
        "owner_id": 2,
        "name": "node-test-device",
        "display_name": "Test device",
        "service_id": "vless-reality",
        "access_policy": {"traffic_limit_bytes": 1024},
        "reason": "Operator migration test",
    }


@pytest.mark.parametrize("role", list(AdminRole))
def test_node_privileged_permissions_are_explicit(role):
    permitted = role in {AdminRole.OWNER, AdminRole.OPS}
    assert (AdminPermission.CONFIGS_EXPORT in permissions_for_role(role)) is permitted
    assert (
        AdminPermission.CONFIGS_PROVISION in permissions_for_role(role)
    ) is permitted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role", [AdminRole.SUPPORT, AdminRole.VIEWER, AdminRole.FINANCE]
)
async def test_metadata_roles_cannot_create_or_export(monkeypatch, sessionmaker, role):
    calls = []

    class Service:
        async def create_config(self, **kwargs):
            calls.append(kwargs)

        async def export_profile(self, config_id):
            calls.append(config_id)

    monkeypatch.setattr(admin_v1_configs, "config_service", Service())
    app = await _app(sessionmaker, role)
    async with _client(app) as client:
        created = await client.post(
            "/api/admin/v1/configs",
            json=_request(),
            headers={"Idempotency-Key": str(uuid4())},
        )
        exported = await client.get("/api/admin/v1/configs/123/profile")
    assert created.status_code == 403
    assert exported.status_code == 403
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [AdminRole.OWNER, AdminRole.OPS])
async def test_explicit_profile_export_is_typed_uncached_and_audited(
    monkeypatch,
    sessionmaker,
    role,
):
    class Service:
        async def export_profile(self, config_id):
            assert config_id == 123
            return NodeProfile(REMOTE_ID, "vless-reality", SECRET_URI)

    monkeypatch.setattr(admin_v1_configs, "config_service", Service())
    app = await _app(sessionmaker, role)
    async with _client(app) as client:
        response = await client.get("/api/admin/v1/configs/123/profile")
    assert response.status_code == 200
    assert response.json() == {
        "config_id": 123,
        "access_id": REMOTE_ID,
        "service_id": "vless-reality",
        "format": "uri",
        "uri": SECRET_URI,
        "content": None,
        "contains_secret": True,
    }
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    async with sessionmaker() as session:
        events = (await session.scalars(select(AdminAuditEvent))).all()
    assert len(events) == 1
    assert events[0].action == "config.export_profile"
    assert events[0].target_id == "123"
    assert SECRET_URI not in json.dumps(events[0].details)


@pytest.mark.asyncio
async def test_profile_errors_cannot_echo_secret(monkeypatch, sessionmaker):
    class Service:
        async def export_profile(self, config_id):
            raise APITransportError("Unexpected upstream text " + SECRET_URI)

    monkeypatch.setattr(admin_v1_configs, "config_service", Service())
    app = await _app(sessionmaker, AdminRole.OPS)
    async with _client(app) as client:
        response = await client.get("/api/admin/v1/configs/123/profile")
    assert response.status_code == 503
    assert SECRET_URI not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_technical_provision_passes_stable_operation_and_no_secret_metadata(
    monkeypatch,
    sessionmaker,
):
    calls = []
    operation_id = str(uuid4())

    class Service:
        async def create_config(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                id=123,
                service_id="vless-reality",
                remote_access_id=REMOTE_ID,
                desired_state="active",
                actual_state="active",
                operation_status="succeeded",
            )

    monkeypatch.setattr(admin_v1_configs, "config_service", Service())
    app = await _app(sessionmaker, AdminRole.OPS)
    async with _client(app) as client:
        responses = [
            await client.post(
                "/api/admin/v1/configs",
                json=_request(),
                headers={"Idempotency-Key": operation_id},
            )
            for _ in range(2)
        ]
    assert all(response.status_code == 201 for response in responses)
    assert responses[0].json() == responses[1].json()
    assert calls[0] == calls[1]
    assert calls[0]["operation_id"] == operation_id
    assert calls[0]["access_policy"] == {
        "traffic_limit_bytes": 1024,
        "activation_days": None,
        "expires_at": None,
    }
    assert "use_password" not in calls[0]
    async with sessionmaker() as session:
        events = (await session.scalars(select(AdminAuditEvent))).all()
    assert len(events) == 2
    assert all(event.action == "config.provision" for event in events)
    assert all(SECRET_URI not in json.dumps(event.details) for event in events)
    assert all("uri" not in response.json() for response in responses)


@pytest.mark.asyncio
async def test_technical_provision_still_requires_csrf(monkeypatch, sessionmaker):
    app = await _app(sessionmaker, AdminRole.OPS)
    async with _client(app) as client:
        client.headers.pop("X-CSRF-Token")
        response = await client.post(
            "/api/admin/v1/configs",
            json=_request(),
            headers={"Idempotency-Key": str(uuid4())},
        )
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation_status", "expected_code", "expected_state"),
    [
        ("pending", 202, "queued_for_reconciliation"),
        ("failed", 202, "queued_for_reconciliation"),
        ("rejected", 502, "failed"),
    ],
)
async def test_replayed_unfinished_provision_is_not_reported_completed(
    monkeypatch,
    sessionmaker,
    operation_status,
    expected_code,
    expected_state,
):
    class Service:
        async def create_config(self, **kwargs):
            return SimpleNamespace(
                id=123,
                service_id="vless-reality",
                remote_access_id=None,
                desired_state="active",
                actual_state="provisioning",
                operation_status=operation_status,
            )

    monkeypatch.setattr(admin_v1_configs, "config_service", Service())
    app = await _app(sessionmaker, AdminRole.OPS)
    async with _client(app) as client:
        response = await client.post(
            "/api/admin/v1/configs",
            json=_request(),
            headers={"Idempotency-Key": str(uuid4())},
        )
    assert response.status_code == expected_code
    assert response.json()["state"] == expected_state
    async with sessionmaker() as session:
        event = await session.scalar(select(AdminAuditEvent))
    assert event.details["outcome"] == expected_state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"access_policy": {"password": "not-accepted"}},
        {"access_policy": {"traffic_limit_bytes": True}},
        {"access_policy": {"expires_at": "2027-01-01T00:00:00"}},
        {"access_policy": {"activation_days": 7}},
        {
            "service_id": "hysteria",
            "access_policy": {"expires_at": "2027-01-01T00:00:00Z"},
        },
        {"use_password": True},
    ],
)
async def test_node_creation_rejects_wrong_or_unknown_policy(
    monkeypatch,
    sessionmaker,
    change,
):
    app = await _app(sessionmaker, AdminRole.OPS)
    body = _request() | change
    async with _client(app) as client:
        response = await client.post(
            "/api/admin/v1/configs",
            json=body,
            headers={"Idempotency-Key": str(uuid4())},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_creation_key_must_be_uuid(monkeypatch, sessionmaker):
    app = await _app(sessionmaker, AdminRole.OPS)
    async with _client(app) as client:
        response = await client.post(
            "/api/admin/v1/configs",
            json=_request(),
            headers={"Idempotency-Key": "different-key-each-time"},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_node_metadata_lists_never_export_profile(monkeypatch, sessionmaker):
    async with sessionmaker() as session, session.begin():
        user = User(tg_id=12345, username="node-test-owner")
        server = Server(
            name="NL",
            ip="127.0.0.1",
            port=18010,
            host="node.test",
            location="NL",
            api_key="unused",
            backend_type="node-agent",
            node_id="nl-1",
        )
        session.add_all([user, server])
        await session.flush()
        session.add(
            VPN_Config(
                name="private-profile",
                server_id=server.id,
                owner_id=user.id,
                display_name="Device",
                service_id="vless-reality",
                actual_state="limited",
            )
        )
    app = await _app(sessionmaker, AdminRole.VIEWER)
    async with _client(app) as client:
        response = await client.get("/api/admin/v1/configs?state=limited")
    assert response.status_code == 200
    assert response.json()["total"] == 1
    item = response.json()["items"][0]
    assert item["service_id"] == "vless-reality"
    assert item["actual_state"] == "limited"
    assert "uri" not in item
    assert SECRET_URI not in response.text
