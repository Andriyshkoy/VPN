from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from admin.fleet_schemas import (
    AdminServerActionRequest,
    AdminServerCreate,
    AdminServerUpdate,
)
from admin.fleet_service import AdminFleetService
from admin.security import AdminPrincipal, AdminRole
from core.db.models import AdminUser, VPNServerStatus
from core.db.unit_of_work import uow
from core.exceptions import InvalidOperationError
from core.services import ServerService, UserService
from core.services.fleet_placement import manager_readiness_decision
from core.services.node_gateway import NodeReadiness
from core.services.vpn_drift import VPNDriftService


def _payload(**extra):
    return dict(
        name="NL",
        ip="127.0.0.1",
        port=18443,
        host="nl.test",
        location="NL",
        api_key="test-operator-token",
        **extra,
    )


async def _node(**extra):
    return await ServerService(uow).create(
        **_payload(), cost=0, backend_type="node-agent", node_id="nl-1", **extra
    )


def _request():
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/api/admin/v1/servers/1/actions",
            "headers": [(b"host", b"admin.test")],
            "client": ("127.0.0.1", 12345),
            "server": ("admin.test", 443),
        }
    )
    request.state.request_id = "node-fleet-test"
    request.state.correlation_id = "node-fleet-test"
    return request


async def _principal(sessionmaker):
    async with sessionmaker() as session, session.begin():
        admin = AdminUser(
            username="node-owner", password_hash="unused", role=AdminRole.OWNER.value
        )
        session.add(admin)
        await session.flush()
        admin_id = admin.id
    return AdminPrincipal(
        user_id=admin_id,
        username="node-owner",
        role=AdminRole.OWNER,
        session_id=1,
        csrf_token_hash="x" * 64,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


def _snapshot(node_id="nl-1", ready=True):
    return {
        "backend_type": "node-agent",
        "node_id": node_id,
        "endpoint_revision": 1,
        "readiness": {"ready": ready, "scope": "control_plane"},
        "data_plane": {"status": "unknown", "verified": False},
    }


class NodeGateway:
    calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def ready(self):
        self.calls += 1
        return NodeReadiness(
            node_id="nl-1",
            ready=True,
            database_ready=True,
            services={"hysteria": True, "vless-reality": True},
        )


def test_node_registration_requires_explicit_identity():
    assert AdminServerCreate(**_payload()).backend_type == "openvpn-manager"
    with pytest.raises(ValidationError):
        AdminServerCreate(**_payload(backend_type="node-agent"))
    with pytest.raises(ValidationError):
        AdminServerCreate(**_payload(node_id="nl-1"))
    created = AdminServerCreate(**_payload(backend_type="node-agent", node_id="nl-1"))
    assert created.lifecycle_state == "disabled"
    assert created.accepts_new_configs is False


def test_node_placement_distinguishes_readiness_from_data_plane():
    now = datetime.now(timezone.utc)
    server = SimpleNamespace(
        backend_type="node-agent", node_id="nl-1", manager_instance_id=None
    )
    latest = SimpleNamespace(
        success=True, collected_at=now, snapshot=_snapshot(), manager_instance_id=None
    )
    assert manager_readiness_decision(server, latest, now=now).allowed
    assert latest.snapshot["data_plane"]["status"] == "unknown"
    latest.snapshot = _snapshot(node_id="other-node")
    assert (
        manager_readiness_decision(server, latest, now=now).reason
        == "node_identity_mismatch"
    )
    latest.snapshot = _snapshot(ready=False)
    assert (
        manager_readiness_decision(server, latest, now=now).reason == "node_unhealthy"
    )
    latest.snapshot = _snapshot()
    latest.collected_at = now - timedelta(hours=1)
    assert (
        manager_readiness_decision(server, latest, now=now).reason
        == "health_check_required"
    )


@pytest.mark.asyncio
async def test_node_poll_keeps_data_plane_and_manager_identity_unknown(sessionmaker):
    node = await _node(lifecycle_state="active", accepts_new_configs=True)
    gateway = NodeGateway()

    def legacy_factory(*args, **kwargs):
        raise AssertionError("OpenVPN gateway must not be contacted")

    service = AdminFleetService(
        gateway_factory=legacy_factory,
        node_gateway_factory=lambda *args, **kwargs: gateway,
    )
    assert await service.poll_server_statuses() == {
        "checked": 1,
        "succeeded": 1,
        "failed": 0,
    }
    status = await service.get_latest_status(node.id)
    assert status["health_scope"] == "control_plane"
    assert status["manager_ready"] is True
    assert status["openvpn_ready"] is None
    assert status["data_plane_verified"] is False
    assert status["data_plane_status"] == "unknown"
    assert status["manager_instance_id"] is None
    assert status["certificate_expires_at"] is None
    assert status["status"] == "healthy"
    assert await ServerService(uow).accepts_new_config(node.id) is True


@pytest.mark.asyncio
async def test_legacy_available_menu_excludes_nodes_unless_explicit(sessionmaker):
    node = await _node(lifecycle_state="active", accepts_new_configs=True)
    async with uow() as repos:
        repos["servers"].session.add(
            VPNServerStatus(
                server_id=node.id,
                kind="status",
                success=True,
                snapshot=_snapshot(),
                collected_at=datetime.now(timezone.utc),
            )
        )
    assert await ServerService(uow).list(available_only=True) == []
    assert [
        row.id
        for row in await ServerService(uow).list(
            available_only=True, backend_type="node-agent"
        )
    ] == [node.id]
    assert [row.id for row in await ServerService(uow).list()] == [node.id]


@pytest.mark.asyncio
async def test_node_identity_endpoint_change_requires_drain(sessionmaker):
    node = await _node()
    user = await UserService(uow).register(999123)
    async with uow() as repos:
        await repos["configs"].create(node.id, user.id, "node-profile", "Device")
    with pytest.raises(InvalidOperationError, match="Drain"):
        await ServerService(uow).update(node.id, node_id="nl-2")
    stored = await ServerService(uow).get(node.id)
    assert stored.node_id == "nl-1"
    with pytest.raises(InvalidOperationError, match="Drain"):
        await AdminFleetService().update_server(
            node.id,
            request=_request(),
            principal=await _principal(sessionmaker),
            data=AdminServerUpdate(expected_version=1, node_id="nl-2"),
        )


@pytest.mark.asyncio
async def test_unoccupied_node_identity_change_quarantines(sessionmaker):
    node = await _node(lifecycle_state="active", accepts_new_configs=True)
    updated = await ServerService(uow).update(node.id, node_id="nl-2")
    assert updated.node_id == "nl-2"
    assert updated.lifecycle_state == "disabled"
    assert updated.accepts_new_configs is False
    assert updated.manager_instance_id is None


@pytest.mark.asyncio
async def test_node_inventory_and_drift_reject_before_legacy_calls(sessionmaker):
    node = await _node()
    principal = await _principal(sessionmaker)

    def no_remote(*args, **kwargs):
        raise AssertionError("No legacy endpoint may be called")

    service = AdminFleetService(gateway_factory=no_remote)
    for action in ("refresh_inventory", "audit_drift"):
        with pytest.raises(InvalidOperationError, match="unsupported"):
            await service.execute_action(
                node.id,
                request=_request(),
                principal=principal,
                client_key="node-unsupported-" + action,
                command=AdminServerActionRequest(action=action),
            )
    with pytest.raises(InvalidOperationError, match="unsupported"):
        await VPNDriftService(uow, gateway_factory=no_remote).audit_server(node.id)


@pytest.mark.asyncio
async def test_old_readiness_cannot_activate_rotated_endpoint_and_regular_actions_stay_valid(
    sessionmaker,
):
    node = await _node(lifecycle_state="active", accepts_new_configs=True)
    principal = await _principal(sessionmaker)
    service = AdminFleetService(
        node_gateway_factory=lambda *args, **kwargs: NodeGateway()
    )
    assert (await service.poll_server_statuses())["succeeded"] == 1
    updated = await service.update_server(
        node.id,
        request=_request(),
        principal=principal,
        data=AdminServerUpdate(expected_version=1, api_key="new-test-operator-token"),
    )
    assert updated["endpoint_revision"] == 2
    assert updated["lifecycle_state"] == "disabled"
    with pytest.raises(InvalidOperationError, match="Node identity"):
        await service.execute_action(
            node.id,
            request=_request(),
            principal=principal,
            client_key="activate-stale-node-endpoint",
            command=AdminServerActionRequest(action="activate", expected_version=2),
        )
    assert (await service.poll_server_statuses())["succeeded"] == 1
    await service.execute_action(
        node.id,
        request=_request(),
        principal=principal,
        client_key="activate-current-node-endpoint",
        command=AdminServerActionRequest(action="activate", expected_version=2),
    )
    await service.execute_action(
        node.id,
        request=_request(),
        principal=principal,
        client_key="enable-current-node-endpoint",
        command=AdminServerActionRequest(
            action="enable_new_configs", expected_version=3
        ),
    )
    stored = await ServerService(uow).get(node.id)
    assert stored.version == 4
    assert stored.endpoint_revision == 2
    assert await ServerService(uow).accepts_new_config(node.id) is True
