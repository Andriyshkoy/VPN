from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import bcrypt
import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from starlette.requests import Request

from admin.fleet_schemas import AdminServerActionRequest, AdminServerUpdate
from admin.fleet_service import (
    AdminFleetService,
    FleetIdempotencyConflict,
    FleetOptimisticConflict,
)
from admin.routers import admin_v1_fleet
from admin.security import AdminPrincipal, AdminRole, login_rate_limiter
from core.db.models import (
    AdminAction,
    AdminAuditEvent,
    AdminUser,
    Server,
    VPNServerStatus,
)
from core.db.unit_of_work import uow
from core.domain import AdminActionStatus
from core.exceptions import InvalidOperationError, ServerNotFoundError
from core.services import ServerService
from core.services.api_gateway import (
    APIGateway,
    ManagerClientInventory,
    ManagerClientState,
    ManagerDataPlaneStatus,
    ManagerExpiryStatus,
    ManagerFleetStatus,
    ManagerInventoryCounts,
    ManagerInventoryStatus,
    ManagerPKIStatus,
    ManagerReadiness,
)


def _request() -> Request:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/api/admin/v1/servers/1/actions",
            "headers": [(b"host", b"admin.test"), (b"user-agent", b"pytest")],
            "client": ("127.0.0.1", 12345),
            "server": ("admin.test", 443),
        }
    )
    request.state.request_id = "fleet-request"
    request.state.correlation_id = "fleet-correlation"
    return request


async def _admin(sessionmaker, *, role: AdminRole = AdminRole.OWNER):
    async with sessionmaker() as session, session.begin():
        user = AdminUser(
            username=f"fleet-{role.value}",
            password_hash=bcrypt.hashpw(b"password", bcrypt.gensalt()).decode(),
            role=role.value,
        )
        session.add(user)
        await session.flush()
        user_id = user.id
    return AdminPrincipal(
        user_id=user_id,
        username=f"fleet-{role.value}",
        role=role,
        session_id=1,
        csrf_token_hash="x" * 64,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


async def _server():
    return await ServerService(uow).create(
        name="Amsterdam",
        ip="manager.test",
        port=16290,
        host="nl.example.test",
        location="NL",
        api_key="top-secret-manager-key",
        cost=Decimal("12.00"),
        max_configs=100,
        capacity_reserve=5,
        public_endpoint="vpn.example.test:1194",
        lifecycle_state="active",
        accepts_new_configs=True,
    )


def _manager_status() -> ManagerFleetStatus:
    observed = datetime(2026, 7, 14, tzinfo=timezone.utc)
    expiry = ManagerExpiryStatus(
        status="valid",
        expires_at=observed + timedelta(days=30),
        remaining_seconds=30 * 86400,
    )
    return ManagerFleetStatus(
        manager_version="1.3.0",
        instance_id="56c1ab62-0c42-4f03-83c6-4c8e6c43e29b",
        observed_at=observed,
        readiness=ManagerReadiness(ready=True, errors=()),
        inventory=ManagerInventoryStatus(
            availability="available",
            revision="sha256:inventory",
            collected_at=observed,
            age_seconds=0,
            counts=ManagerInventoryCounts(
                total=2,
                active=1,
                suspended=1,
                revoked=0,
                expired=0,
                incomplete=0,
                orphaned=0,
                unknown=0,
            ),
        ),
        data_plane=ManagerDataPlaneStatus(
            status="up",
            online_sessions=1,
            bytes_received=123,
            bytes_sent=456,
            status_file_age_seconds=2,
        ),
        pki=ManagerPKIStatus(server_certificate=expiry, crl=expiry),
    )


class FleetGateway:
    def __init__(self):
        self.status_calls = 0
        self.inventory_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get_status(self):
        self.status_calls += 1
        return _manager_status()

    async def get_client_inventory(self, *, etag=None):
        self.inventory_calls += 1
        client = ManagerClientState(
            name="must-not-be-persisted",
            state="active",
            certificate_status="valid",
            index_statuses=("V",),
            suspended=False,
            config_present=True,
            config_complete=True,
            certificate_present=True,
            private_key_present=True,
            manageable=True,
            issues=(),
        )
        return ManagerClientInventory(
            revision="sha256:inventory", count=1, clients=(client,)
        )


@pytest.mark.asyncio
async def test_background_fleet_poll_persists_bounded_status(sessionmaker):
    server = await _server()
    gateway = FleetGateway()
    service = AdminFleetService(gateway_factory=lambda *args, **kwargs: gateway)

    result = await service.poll_server_statuses()

    assert result == {"checked": 1, "succeeded": 1, "failed": 0}
    assert gateway.status_calls == 1
    latest = await service.get_latest_status(server.id)
    assert latest is not None
    assert latest["status"] == "healthy"
    assert latest["online_sessions"] == 1
    async with sessionmaker() as session:
        persisted = await session.get(Server, server.id)
        assert persisted.manager_instance_id == _manager_status().instance_id


@pytest.mark.asyncio
async def test_status_action_is_typed_durable_idempotent_and_secret_free(
    sessionmaker,
):
    principal = await _admin(sessionmaker)
    server = await _server()
    gateway = FleetGateway()
    service = AdminFleetService(gateway_factory=lambda *args, **kwargs: gateway)
    command = AdminServerActionRequest(
        action="refresh_status", reason="manual health check"
    )

    first = await service.execute_action(
        server.id,
        request=_request(),
        principal=principal,
        client_key="fleet-health-key",
        command=command,
    )
    replay = await service.execute_action(
        server.id,
        request=_request(),
        principal=principal,
        client_key="fleet-health-key",
        command=command,
    )

    assert first["status"] == "succeeded"
    assert replay["replayed"] is True
    assert gateway.status_calls == 1
    detail = await service.get_server(server.id)
    latest = await service.get_latest_status(server.id)
    assert detail["api_key_configured"] is True
    assert "api_key" not in detail
    assert latest["status"] == "healthy"
    assert latest["online_sessions"] == 1
    assert "top-secret-manager-key" not in str(first)

    async with sessionmaker() as session:
        assert await session.scalar(select(func.count(AdminAction.id))) == 1
        status_row = await session.scalar(select(VPNServerStatus))
        assert status_row is not None
        assert status_row.manager_instance_id == _manager_status().instance_id
        assert "top-secret-manager-key" not in str(status_row.snapshot)


@pytest.mark.asyncio
async def test_inventory_snapshot_contains_aggregates_not_client_names(sessionmaker):
    principal = await _admin(sessionmaker)
    server = await _server()
    gateway = FleetGateway()
    service = AdminFleetService(gateway_factory=lambda *args, **kwargs: gateway)

    result = await service.execute_action(
        server.id,
        request=_request(),
        principal=principal,
        client_key="fleet-inventory-key",
        command=AdminServerActionRequest(
            action="refresh_inventory", reason="refresh inventory"
        ),
    )

    assert result["result"]["count"] == 1
    async with sessionmaker() as session:
        row = await session.scalar(
            select(VPNServerStatus).where(VPNServerStatus.kind == "inventory")
        )
        assert row.snapshot["counts"]["active"] == 1
        assert "must-not-be-persisted" not in str(row.snapshot)


@pytest.mark.asyncio
async def test_inventory_refresh_does_not_replace_latest_health_snapshot(sessionmaker):
    principal = await _admin(sessionmaker)
    server = await _server()
    gateway = FleetGateway()
    service = AdminFleetService(gateway_factory=lambda *args, **kwargs: gateway)
    await service.execute_action(
        server.id,
        request=_request(),
        principal=principal,
        client_key="status-before-inventory",
        command=AdminServerActionRequest(
            action="refresh_status", reason="capture health"
        ),
    )
    await service.execute_action(
        server.id,
        request=_request(),
        principal=principal,
        client_key="inventory-after-status",
        command=AdminServerActionRequest(
            action="refresh_inventory", reason="capture inventory"
        ),
    )

    latest = await service.get_latest_status(server.id)
    assert latest["status"] == "healthy"
    assert latest["online_sessions"] == 1
    assert latest["inventory_revision"] == "sha256:inventory"


@pytest.mark.asyncio
async def test_old_status_is_stale_and_health_filter_paginates_after_filtering(
    monkeypatch, sessionmaker
):
    principal = await _admin(sessionmaker)
    server = await _server()
    gateway = FleetGateway()
    service = AdminFleetService(gateway_factory=lambda *args, **kwargs: gateway)
    await service.execute_action(
        server.id,
        request=_request(),
        principal=principal,
        client_key="status-that-becomes-stale",
        command=AdminServerActionRequest(
            action="refresh_status", reason="capture status"
        ),
    )
    monkeypatch.setattr(
        "admin.fleet_service.settings.admin_fleet_status_stale_seconds", 30
    )
    async with sessionmaker() as session, session.begin():
        row = await session.scalar(
            select(VPNServerStatus).where(VPNServerStatus.kind == "status")
        )
        row.collected_at = datetime.now(timezone.utc) - timedelta(minutes=5)

    latest = await service.get_latest_status(server.id)
    page = await service.list_servers(health_state="stale", limit=1, offset=0)
    empty_page = await service.list_servers(health_state="stale", limit=1, offset=1)
    assert latest["status"] == "stale"
    assert page["total"] == 1
    assert [item["id"] for item in page["items"]] == [server.id]
    assert empty_page["total"] == 1 and empty_page["items"] == []


@pytest.mark.asyncio
async def test_recover_stale_running_action_marks_it_failed_and_audits(
    monkeypatch, sessionmaker
):
    principal = await _admin(sessionmaker)
    server = await _server()
    monkeypatch.setattr("admin.fleet_service.settings.admin_action_stale_seconds", 120)
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    async with sessionmaker() as session, session.begin():
        action = AdminAction(
            server_id=server.id,
            actor_user_id=principal.user_id,
            kind="refresh_status",
            status=AdminActionStatus.RUNNING.value,
            idempotency_key_hash="a" * 64,
            request_hash="b" * 64,
            reason="interrupted request",
            payload={},
            result={},
            started_at=old,
            created_at=old,
        )
        session.add(action)
        await session.flush()
        action_id = action.action_id

    assert await AdminFleetService().recover_stale_actions() == 1
    async with sessionmaker() as session:
        recovered = await session.scalar(
            select(AdminAction).where(AdminAction.action_id == action_id)
        )
        assert recovered.status == "failed"
        assert recovered.error_code == "stale_action_recovered"
        event = await session.scalar(
            select(AdminAuditEvent).where(
                AdminAuditEvent.action == "server.refresh_status.recovered"
            )
        )
        assert event.details["admin_action_id"] == action_id


@pytest.mark.asyncio
async def test_lifecycle_actions_require_version_and_idempotency(sessionmaker):
    principal = await _admin(sessionmaker)
    server = await _server()
    service = AdminFleetService()
    request = _request()

    with pytest.raises(FleetOptimisticConflict, match="expected_version"):
        await service.execute_action(
            server.id,
            request=request,
            principal=principal,
            client_key="fleet-drain-no-version",
            command=AdminServerActionRequest(action="drain", reason="maintenance"),
        )

    command = AdminServerActionRequest(
        action="drain", reason="maintenance", expected_version=1
    )
    result = await service.execute_action(
        server.id,
        request=request,
        principal=principal,
        client_key="fleet-drain-key",
        command=command,
    )
    replay = await service.execute_action(
        server.id,
        request=request,
        principal=principal,
        client_key="fleet-drain-key",
        command=command,
    )
    assert result["result"]["lifecycle_state"] == "draining"
    assert result["result"]["version"] == 2
    assert replay["replayed"] is True

    with pytest.raises(FleetIdempotencyConflict):
        await service.execute_action(
            server.id,
            request=request,
            principal=principal,
            client_key="fleet-drain-key",
            command=AdminServerActionRequest(
                action="disable", reason="different request", expected_version=2
            ),
        )
    with pytest.raises(FleetOptimisticConflict, match="current 2"):
        await service.execute_action(
            server.id,
            request=request,
            principal=principal,
            client_key="fleet-disable-stale",
            command=AdminServerActionRequest(
                action="disable", reason="stale request", expected_version=1
            ),
        )
    with pytest.raises(InvalidOperationError, match="accepts_new_configs"):
        await service.execute_action(
            server.id,
            request=request,
            principal=principal,
            client_key="fleet-accepting-missing-value",
            command=AdminServerActionRequest(
                action="set_accepting",
                reason="missing required action value",
                expected_version=2,
            ),
        )

    async with sessionmaker() as session:
        event = await session.scalar(
            select(AdminAuditEvent).where(AdminAuditEvent.action == "server.drain")
        )
        assert event is not None
        assert event.details["reason"] == "maintenance"
        rejected = (
            await session.scalars(
                select(AdminAuditEvent)
                .where(AdminAuditEvent.action.like("server.%.rejected"))
                .order_by(AdminAuditEvent.id)
            )
        ).all()
        assert [item.details["error_code"] for item in rejected] == [
            "optimistic_conflict",
            "idempotency_conflict",
            "optimistic_conflict",
            "invalid_operation",
        ]
        serialized = str([item.details for item in rejected])
        for raw_key in (
            "fleet-drain-no-version",
            "fleet-drain-key",
            "fleet-disable-stale",
            "fleet-accepting-missing-value",
            "top-secret-manager-key",
        ):
            assert raw_key not in serialized
        assert all(len(item.details["idempotency_key_hash"]) == 64 for item in rejected)


@pytest.mark.asyncio
async def test_rejected_server_update_is_audited_without_secret_values(sessionmaker):
    principal = await _admin(sessionmaker)
    server = await _server()
    service = AdminFleetService()
    replacement_key = "replacement-manager-secret"

    with pytest.raises(FleetOptimisticConflict, match="current 1"):
        await service.update_server(
            server.id,
            request=_request(),
            principal=principal,
            data=AdminServerUpdate(
                expected_version=99,
                name="Rejected rename",
                api_key=replacement_key,
            ),
        )

    async with sessionmaker() as session:
        event = await session.scalar(
            select(AdminAuditEvent).where(
                AdminAuditEvent.action == "server.update.rejected"
            )
        )
        assert event is not None
        assert event.details == {
            "outcome": "rejected",
            "error_code": "optimistic_conflict",
            "expected_version": 99,
            "changed_fields": ["api_key", "name"],
            "api_key_updated": True,
        }
        assert replacement_key not in str(event.details)
        assert "top-secret-manager-key" not in str(event.details)


@pytest.mark.asyncio
async def test_remote_preflight_rejections_are_audited_without_remote_calls_or_keys(
    sessionmaker,
):
    principal = await _admin(sessionmaker)
    server = await _server()
    gateway = FleetGateway()
    service = AdminFleetService(gateway_factory=lambda *args, **kwargs: gateway)

    with pytest.raises(FleetOptimisticConflict, match="current 1"):
        await service.execute_action(
            server.id,
            request=_request(),
            principal=principal,
            client_key="remote-stale-version-key",
            command=AdminServerActionRequest(
                action="refresh_status",
                reason="stale remote read",
                expected_version=99,
            ),
        )
    assert gateway.status_calls == 0

    first_command = AdminServerActionRequest(
        action="refresh_status", reason="first remote read"
    )
    await service.execute_action(
        server.id,
        request=_request(),
        principal=principal,
        client_key="remote-shared-key",
        command=first_command,
    )
    with pytest.raises(FleetIdempotencyConflict):
        await service.execute_action(
            server.id,
            request=_request(),
            principal=principal,
            client_key="remote-shared-key",
            command=AdminServerActionRequest(
                action="refresh_status", reason="different remote request"
            ),
        )
    assert gateway.status_calls == 1

    with pytest.raises(ServerNotFoundError):
        await service.execute_action(
            server.id + 999,
            request=_request(),
            principal=principal,
            client_key="remote-missing-server-key",
            command=AdminServerActionRequest(
                action="refresh_status", reason="missing remote server"
            ),
        )
    assert gateway.status_calls == 1

    async with sessionmaker() as session:
        rejected = (
            await session.scalars(
                select(AdminAuditEvent)
                .where(AdminAuditEvent.action == "server.refresh_status.rejected")
                .order_by(AdminAuditEvent.id)
            )
        ).all()
        assert [item.details["error_code"] for item in rejected] == [
            "optimistic_conflict",
            "idempotency_conflict",
            "server_not_found",
        ]
        actions = (
            await session.scalars(select(AdminAction).order_by(AdminAction.id))
        ).all()
        assert len(actions) == 1, [
            (item.kind, item.status, item.expected_server_version) for item in actions
        ]
        serialized = str([item.details for item in rejected])
        for raw_key in (
            "remote-stale-version-key",
            "remote-shared-key",
            "remote-missing-server-key",
            "top-secret-manager-key",
        ):
            assert raw_key not in serialized


@pytest.mark.asyncio
async def test_activation_requires_fresh_healthy_matching_manager_status(sessionmaker):
    principal = await _admin(sessionmaker)
    server = await _server()
    gateway = FleetGateway()
    service = AdminFleetService(gateway_factory=lambda *args, **kwargs: gateway)
    async with sessionmaker() as session, session.begin():
        persisted = await session.get(Server, server.id)
        persisted.lifecycle_state = "disabled"
        persisted.accepts_new_configs = False

    with pytest.raises(InvalidOperationError, match="fresh successful"):
        await service.execute_action(
            server.id,
            request=_request(),
            principal=principal,
            client_key="activate-without-health",
            command=AdminServerActionRequest(
                action="activate", reason="enable verified node", expected_version=1
            ),
        )

    await service.execute_action(
        server.id,
        request=_request(),
        principal=principal,
        client_key="activation-health-check-one",
        command=AdminServerActionRequest(
            action="refresh_status", reason="verify node health"
        ),
    )
    async with sessionmaker() as session, session.begin():
        status_row = await session.scalar(
            select(VPNServerStatus)
            .where(VPNServerStatus.kind == "status")
            .order_by(VPNServerStatus.id.desc())
        )
        status_row.collected_at = datetime.now(timezone.utc) - timedelta(hours=1)

    with pytest.raises(InvalidOperationError, match="fresh successful"):
        await service.execute_action(
            server.id,
            request=_request(),
            principal=principal,
            client_key="activate-with-stale-health",
            command=AdminServerActionRequest(
                action="activate", reason="enable verified node", expected_version=1
            ),
        )

    await service.execute_action(
        server.id,
        request=_request(),
        principal=principal,
        client_key="activation-health-check-two",
        command=AdminServerActionRequest(
            action="refresh_status", reason="refresh node health"
        ),
    )
    async with sessionmaker() as session, session.begin():
        persisted = await session.get(Server, server.id)
        persisted.manager_instance_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

    with pytest.raises(InvalidOperationError, match="identity must match"):
        await service.execute_action(
            server.id,
            request=_request(),
            principal=principal,
            client_key="activate-with-wrong-instance",
            command=AdminServerActionRequest(
                action="activate", reason="enable verified node", expected_version=1
            ),
        )

    async with sessionmaker() as session, session.begin():
        persisted = await session.get(Server, server.id)
        persisted.manager_instance_id = _manager_status().instance_id

    activated = await service.execute_action(
        server.id,
        request=_request(),
        principal=principal,
        client_key="activate-after-verification",
        command=AdminServerActionRequest(
            action="activate", reason="enable verified node", expected_version=1
        ),
    )
    assert activated["result"]["lifecycle_state"] == "active"
    assert activated["result"]["accepts_new_configs"] is True

    async with sessionmaker() as session:
        rejected = (
            await session.scalars(
                select(AdminAuditEvent)
                .where(AdminAuditEvent.action == "server.activate.rejected")
                .order_by(AdminAuditEvent.id)
            )
        ).all()
        assert len(rejected) == 3
        assert all(
            item.details["error_code"] == "invalid_operation" for item in rejected
        )
        assert not await session.scalar(
            select(func.count(AdminAction.id)).where(
                AdminAction.status == AdminActionStatus.RUNNING.value
            )
        )


@pytest.mark.asyncio
async def test_reopening_placement_requires_fresh_healthy_status(sessionmaker):
    principal = await _admin(sessionmaker)
    server = await _server()
    gateway = FleetGateway()
    service = AdminFleetService(gateway_factory=lambda *args, **kwargs: gateway)

    closed = await service.execute_action(
        server.id,
        request=_request(),
        principal=principal,
        client_key="close-placement-before-health",
        command=AdminServerActionRequest(
            action="set_accepting",
            accepts_new_configs=False,
            reason="pause new placements",
            expected_version=1,
        ),
    )
    assert closed["result"]["accepts_new_configs"] is False

    with pytest.raises(InvalidOperationError, match="fresh successful"):
        await service.execute_action(
            server.id,
            request=_request(),
            principal=principal,
            client_key="reopen-placement-without-health",
            command=AdminServerActionRequest(
                action="set_accepting",
                accepts_new_configs=True,
                reason="resume new placements",
                expected_version=2,
            ),
        )

    await service.execute_action(
        server.id,
        request=_request(),
        principal=principal,
        client_key="verify-before-reopening-placement",
        command=AdminServerActionRequest(
            action="refresh_status",
            reason="verify node before placement",
        ),
    )
    reopened = await service.execute_action(
        server.id,
        request=_request(),
        principal=principal,
        client_key="reopen-placement-after-health",
        command=AdminServerActionRequest(
            action="set_accepting",
            accepts_new_configs=True,
            reason="resume new placements",
            expected_version=2,
        ),
    )
    assert reopened["result"]["accepts_new_configs"] is True


def _status_http_payload():
    status = _manager_status()
    payload = {
        "manager_version": status.manager_version,
        "instance_id": status.instance_id,
        "observed_at": status.observed_at.isoformat(),
        "readiness": {"ready": True, "errors": []},
        "inventory": {
            "availability": "available",
            "revision": "sha256:inventory",
            "collected_at": status.observed_at.isoformat(),
            "age_seconds": 0,
            "counts": {
                "total": 2,
                "active": 1,
                "suspended": 1,
                "revoked": 0,
                "expired": 0,
                "incomplete": 0,
                "orphaned": 0,
                "unknown": 0,
            },
        },
        "data_plane": {
            "status": "up",
            "online_sessions": 1,
            "bytes_received": 123,
            "bytes_sent": 456,
            "status_file_age_seconds": 2,
        },
        "pki": {
            "server_certificate": {
                "status": "valid",
                "expires_at": status.pki.server_certificate.expires_at.isoformat(),
                "remaining_seconds": 2592000,
            },
            "crl": {
                "status": "valid",
                "expires_at": status.pki.crl.expires_at.isoformat(),
                "remaining_seconds": 2592000,
            },
        },
        # Unknown Manager additions must not be copied into the typed model.
        "private_key": "must-not-pass-through",
    }
    return payload


@pytest.mark.asyncio
async def test_api_gateway_get_status_accepts_contract_and_drops_unknown_fields():
    gateway = APIGateway("manager.test", 16290, "secret", tls_enabled=False, retries=0)
    request = httpx.Request("GET", "http://manager.test:16290/status")

    class Client:
        async def request(self, method, url, **kwargs):
            return httpx.Response(200, request=request, json=_status_http_payload())

    gateway._client = Client()
    parsed = await gateway.get_status()

    assert parsed.readiness.ready is True
    assert parsed.inventory.counts.total == 2
    assert parsed.data_plane.online_sessions == 1
    assert not hasattr(parsed, "private_key")


@pytest.mark.asyncio
async def test_fleet_api_rbac_csrf_write_only_key_and_action(monkeypatch, sessionmaker):
    password_hash = bcrypt.hashpw(b"fleet-password", bcrypt.gensalt()).decode()
    monkeypatch.setenv("ADMIN_USERNAME", "fleet-owner")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", password_hash)
    await login_rate_limiter.clear()

    gateway = FleetGateway()
    monkeypatch.setattr(
        admin_v1_fleet.fleet,
        "_gateway_factory",
        lambda *args, **kwargs: gateway,
    )
    from admin.app import app

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://admin.test",
        headers={"Origin": "https://admin.test"},
    ) as client:
        assert (await client.get("/api/admin/v1/servers")).status_code == 401
        login = await client.post(
            "/api/admin/v1/auth/login",
            json={"username": "fleet-owner", "password": "fleet-password"},
        )
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        missing_csrf = await client.post(
            "/api/admin/v1/servers",
            json={
                "name": "API server",
                "ip": "manager-api.test",
                "host": "vpn-api.test",
                "location": "DE",
                "api_key": "never-return-this",
                "monthly_cost": "10.00",
            },
        )
        assert missing_csrf.status_code == 403
        created = await client.post(
            "/api/admin/v1/servers",
            headers={"X-CSRF-Token": csrf},
            json={
                "name": "API server",
                "ip": "manager-api.test",
                "host": "vpn-api.test",
                "location": "DE",
                "api_key": "never-return-this",
                "monthly_cost": "10.00",
            },
        )
        assert created.status_code == 201
        body = created.json()
        assert body["api_key_configured"] is True
        assert "api_key" not in body
        assert "never-return-this" not in created.text

        action = await client.post(
            f"/api/admin/v1/servers/{body['id']}/actions",
            headers={
                "X-CSRF-Token": csrf,
                "Idempotency-Key": "api-health-check",
            },
            json={"action": "refresh_status", "reason": "API health check"},
        )
        assert action.status_code == 200
        assert action.json()["status"] == "succeeded"
        status_response = await client.get(f"/api/admin/v1/servers/{body['id']}/status")
        assert status_response.status_code == 200
        assert status_response.json()["manager_ready"] is True
