from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from starlette.requests import Request

from admin.fleet_schemas import (
    AdminServerActionRequest,
    AdminServerCreate,
    AdminServerUpdate,
)
from admin.fleet_service import AdminFleetService
from admin.security import AdminPrincipal, AdminRole
from core.db.models import (
    AdminAuditEvent,
    AdminUser,
    Server,
    VPN_Config,
    VPNServerStatus,
)
from core.db.unit_of_work import uow
from core.domain import VPNState
from core.exceptions import InvalidOperationError
from core.services import ConfigService, ServerService, UserService

INSTANCE_ID = "56c1ab62-0c42-4f03-83c6-4c8e6c43e29b"


def _request() -> Request:
    request = Request(
        {
            "type": "http",
            "method": "PATCH",
            "scheme": "https",
            "path": "/api/admin/v1/servers/1",
            "headers": [(b"host", b"admin.test")],
            "client": ("127.0.0.1", 12345),
            "server": ("admin.test", 443),
        }
    )
    request.state.request_id = "placement-request"
    request.state.correlation_id = "placement-correlation"
    return request


async def _principal(sessionmaker) -> AdminPrincipal:
    async with sessionmaker() as session, session.begin():
        admin = AdminUser(
            username="placement-owner",
            password_hash="unused",
            role=AdminRole.OWNER.value,
        )
        session.add(admin)
        await session.flush()
        admin_id = admin.id
    return AdminPrincipal(
        user_id=admin_id,
        username="placement-owner",
        role=AdminRole.OWNER,
        session_id=1,
        csrf_token_hash="x" * 64,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


async def _server(
    sessionmaker,
    *,
    max_configs: int | None = 10,
    manager_instance_id: str | None = INSTANCE_ID,
) -> Server:
    async with sessionmaker() as session, session.begin():
        server = Server(
            name="placement-node",
            ip="manager.test",
            port=16290,
            host="vpn.test",
            location="NL",
            api_key="secret",
            monthly_cost=Decimal("10.00"),
            lifecycle_state="active",
            accepts_new_configs=True,
            max_configs=max_configs,
            manager_instance_id=manager_instance_id,
        )
        session.add(server)
        await session.flush()
        server_id = server.id
    async with sessionmaker() as session:
        return await session.get(Server, server_id)


async def _status(
    sessionmaker,
    server_id: int,
    *,
    success: bool = True,
    collected_at: datetime | None = None,
    instance_id: str | None = INSTANCE_ID,
    ready: bool = True,
    data_plane: str = "up",
) -> None:
    async with sessionmaker() as session, session.begin():
        session.add(
            VPNServerStatus(
                server_id=server_id,
                kind="status",
                success=success,
                collected_at=collected_at or datetime.now(timezone.utc),
                manager_instance_id=instance_id,
                snapshot={
                    "readiness": {"ready": ready},
                    "data_plane": {"status": data_plane},
                },
            )
        )


def _create_payload(**overrides) -> dict:
    payload = {
        "name": "new-node",
        "ip": "new-manager.test",
        "host": "new-vpn.test",
        "location": "DE",
        "api_key": "secret",
        "monthly_cost": "5.00",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_fleet_create_is_always_quarantined(sessionmaker):
    with pytest.raises(ValidationError):
        AdminServerCreate(
            **_create_payload(lifecycle_state="active", accepts_new_configs=True)
        )

    principal = await _principal(sessionmaker)
    created = await AdminFleetService().create_server(
        request=_request(),
        principal=principal,
        data=AdminServerCreate(**_create_payload()),
    )

    assert created["lifecycle_state"] == "disabled"
    assert created["accepts_new_configs"] is False
    assert created["manager_instance_id"] is None


@pytest.mark.asyncio
async def test_legacy_server_create_defaults_to_quarantine(sessionmaker):
    created = await ServerService(uow).create(
        name="legacy-node",
        ip="legacy-manager.test",
        port=16290,
        host="legacy-vpn.test",
        location="NL",
        api_key="secret",
        cost=0,
    )

    assert created.lifecycle_state == "disabled"
    assert created.accepts_new_configs is False
    assert await ServerService(uow).accepts_new_config(created.id) is False


@pytest.mark.asyncio
async def test_endpoint_rotation_quarantines_identity_and_nullable_fields_clear(
    sessionmaker,
):
    principal = await _principal(sessionmaker)
    server = await _server(sessionmaker)

    updated = await AdminFleetService().update_server(
        server.id,
        request=_request(),
        principal=principal,
        data=AdminServerUpdate(
            expected_version=1,
            ip="replacement-manager.test",
            clear_max_configs=True,
            provider="",
            public_endpoint="",
        ),
    )

    assert updated["ip"] == "replacement-manager.test"
    assert updated["lifecycle_state"] == "disabled"
    assert updated["accepts_new_configs"] is False
    assert updated["manager_instance_id"] is None
    assert updated["max_configs"] is None
    assert updated["provider"] is None
    assert updated["public_endpoint"] is None
    async with sessionmaker() as session:
        event = await session.scalar(
            select(AdminAuditEvent).where(AdminAuditEvent.action == "server.updated")
        )
        assert event.details["endpoint_quarantined"] is True
        assert {
            "lifecycle_state",
            "accepts_new_configs",
            "manager_instance_id",
        }.issubset(event.details["changed_fields"])


@pytest.mark.asyncio
async def test_pending_revoke_keeps_endpoint_and_retirement_fenced(sessionmaker):
    principal = await _principal(sessionmaker)
    server = await _server(sessionmaker)
    user = await UserService(uow).register(70101)
    async with sessionmaker() as session, session.begin():
        config = VPN_Config(
            name="pending-revoke",
            server_id=server.id,
            owner_id=user.id,
            display_name="Pending revoke",
            desired_state=VPNState.REVOKED.value,
            actual_state=VPNState.ACTIVE.value,
        )
        session.add(config)
        await session.flush()
        config_id = config.id

    service = AdminFleetService()
    with pytest.raises(InvalidOperationError, match="Drain all VPN configs"):
        await service.update_server(
            server.id,
            request=_request(),
            principal=principal,
            data=AdminServerUpdate(expected_version=1, ip="blocked-manager.test"),
        )
    with pytest.raises(InvalidOperationError, match="managed configs"):
        await service.execute_action(
            server.id,
            request=_request(),
            principal=principal,
            client_key="pending-revoke-retire",
            command=AdminServerActionRequest(
                action="retire",
                reason="retire drained node",
                expected_version=1,
            ),
        )

    async with sessionmaker() as session, session.begin():
        config = await session.get(VPN_Config, config_id)
        config.actual_state = VPNState.REVOKED.value
    rotated = await service.update_server(
        server.id,
        request=_request(),
        principal=principal,
        data=AdminServerUpdate(expected_version=1, ip="replacement-manager.test"),
    )
    assert rotated["ip"] == "replacement-manager.test"
    assert rotated["configs_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_case",
    ["missing", "failed", "stale", "identity_mismatch", "unhealthy"],
)
async def test_placement_fails_closed_on_manager_status(
    status_case,
    sessionmaker,
):
    server = await _server(sessionmaker)
    user = await UserService(uow).register(70200)
    if status_case != "missing":
        await _status(
            sessionmaker,
            server.id,
            success=status_case != "failed",
            collected_at=(
                datetime.now(timezone.utc) - timedelta(hours=1)
                if status_case == "stale"
                else None
            ),
            instance_id=(
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
                if status_case == "identity_mismatch"
                else INSTANCE_ID
            ),
            ready=status_case != "unhealthy",
        )

    servers = ServerService(uow)
    assert await servers.accepts_new_config(server.id) is False
    assert await servers.list(available_only=True) == []
    with pytest.raises(InvalidOperationError, match="not accepting"):
        async with uow() as repos:
            await ConfigService(uow).prepare_config(
                repos=repos,
                operation_id="00000000-0000-4000-8000-000000000001",
                server_id=server.id,
                owner_id=user.id,
                name=f"blocked-{status_case}",
                display_name="Blocked placement",
            )


@pytest.mark.asyncio
async def test_healthy_matching_server_is_eligible_until_capacity_is_really_free(
    sessionmaker,
):
    server = await _server(sessionmaker, max_configs=1)
    await _status(sessionmaker, server.id)
    user = await UserService(uow).register(70300)
    servers = ServerService(uow)

    assert await servers.accepts_new_config(server.id) is True
    assert [item.id for item in await servers.list(available_only=True)] == [server.id]
    async with sessionmaker() as session, session.begin():
        config = VPN_Config(
            name="capacity-pending-revoke",
            server_id=server.id,
            owner_id=user.id,
            display_name="Capacity",
            desired_state=VPNState.REVOKED.value,
            actual_state=VPNState.ACTIVE.value,
        )
        session.add(config)
        await session.flush()
        config_id = config.id

    assert await servers.accepts_new_config(server.id) is False
    async with sessionmaker() as session, session.begin():
        config = await session.get(VPN_Config, config_id)
        config.actual_state = VPNState.REVOKED.value
    assert await servers.accepts_new_config(server.id) is True
