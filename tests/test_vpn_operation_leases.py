from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.config import settings
from core.db.unit_of_work import uow
from core.domain import VPNOperationKind, VPNOperationStatus, VPNState
from core.exceptions import APINotFoundError, APITransportError
from core.services import ConfigService, ServerService, UserService


class LifecycleGateway:
    def __init__(self, behavior: str = "success") -> None:
        self.behavior = behavior
        self.calls: list[tuple[str, str, str | None]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def create_client(self, name, use_password=False, *, operation_id=None):
        self.calls.append(("provision", name, operation_id))
        self._maybe_fail()
        return "/tmp/client.ovpn"

    async def download_config(self, name):
        if self.behavior == "transport":
            raise APINotFoundError("missing", status_code=404)
        return b"config"

    async def suspend_client(self, name, *, operation_id=None):
        self.calls.append(("suspend", name, operation_id))
        self._maybe_fail()

    async def unsuspend_client(self, name, *, operation_id=None):
        self.calls.append(("unsuspend", name, operation_id))
        self._maybe_fail()

    async def revoke_client(self, name, *, operation_id=None):
        self.calls.append(("revoke", name, operation_id))
        self._maybe_fail()

    async def list_blocked(self):
        return []

    def _maybe_fail(self) -> None:
        if self.behavior == "transport":
            raise APITransportError("ambiguous timeout")


async def _user_and_server(tg_id: int = 70001):
    user = await UserService(uow).register(tg_id)
    server = await ServerService(uow).create(
        name="lease-test",
        ip="127.0.0.1",
        port=8080,
        host="vpn.test",
        location="local",
        api_key="secret",
        cost=0,
    )
    return user, server


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed_and_old_worker_is_fenced(sessionmaker):
    now = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    user, server = await _user_and_server()
    operation_id = "00000000-0000-4000-8000-000000000001"
    async with uow() as repos:
        cfg = await repos["configs"].create(
            server.id,
            user.id,
            "leased-config",
            "Leased",
            desired_state=VPNState.SUSPENDED.value,
            actual_state=VPNState.ACTIVE.value,
            operation_id=operation_id,
        )
        await repos["vpn_operations"].create(
            operation_id=operation_id,
            config_id=cfg.id,
            config_name=cfg.name,
            server_id=server.id,
            owner_id=user.id,
            kind=VPNOperationKind.SUSPEND.value,
            next_attempt_at=now,
        )

    async with uow() as repos:
        first = await repos["vpn_operations"].claim(
            operation_id,
            lease_token="00000000-0000-4000-8000-000000000011",
            now=now,
            lease_for=timedelta(seconds=30),
        )
    assert first is not None
    async with uow() as repos:
        assert (
            await repos["vpn_operations"].claim(
                operation_id,
                lease_token="00000000-0000-4000-8000-000000000012",
                now=now + timedelta(seconds=29),
                lease_for=timedelta(seconds=30),
            )
            is None
        )

    reclaimed_at = now + timedelta(seconds=31)
    async with uow() as repos:
        second = await repos["vpn_operations"].claim(
            operation_id,
            lease_token="00000000-0000-4000-8000-000000000013",
            now=reclaimed_at,
            lease_for=timedelta(seconds=30),
        )
    assert second is not None
    assert second.attempts == 2

    async with uow() as repos:
        stale_completion = await repos["vpn_operations"].mark_succeeded(
            operation_id,
            lease_token="00000000-0000-4000-8000-000000000011",
            now=reclaimed_at,
        )
        current_completion = await repos["vpn_operations"].mark_succeeded(
            operation_id,
            lease_token="00000000-0000-4000-8000-000000000013",
            now=reclaimed_at,
        )
    assert stale_completion is None
    assert current_completion is not None
    assert current_completion.status == VPNOperationStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_transport_retry_uses_bounded_exponential_backoff(
    monkeypatch, sessionmaker
):
    now = [datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)]
    gateway = LifecycleGateway("transport")
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *args, **kwargs: gateway
    )
    user, server = await _user_and_server()
    service = ConfigService(
        uow,
        clock=lambda: now[0],
        retry_base_seconds=5,
        retry_max_seconds=20,
    )

    with pytest.raises(APITransportError):
        await service.create_config(
            server_id=server.id,
            owner_id=user.id,
            name="backoff-config",
            display_name="Backoff",
        )
    async with uow() as repos:
        cfg = await repos["configs"].get(name="backoff-config")
        first = await repos["vpn_operations"].get(operation_id=cfg.operation_id)
    assert first.attempts == 1
    assert _as_utc(first.next_attempt_at) == now[0] + timedelta(seconds=5)

    now[0] += timedelta(seconds=4)
    assert await service.reconcile() == {}
    now[0] += timedelta(seconds=1)
    assert list((await service.reconcile()).values()) == ["failed:APITransportError"]
    async with uow() as repos:
        second = await repos["vpn_operations"].get(operation_id=cfg.operation_id)
    assert second.attempts == 2
    assert _as_utc(second.next_attempt_at) == now[0] + timedelta(seconds=10)


@pytest.mark.asyncio
async def test_retry_budget_moves_operation_to_exhausted(monkeypatch, sessionmaker):
    monkeypatch.setattr(settings, "vpn_operation_max_attempts", 2)
    now = [datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)]
    gateway = LifecycleGateway("transport")
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *args, **kwargs: gateway
    )
    user, server = await _user_and_server()
    service = ConfigService(uow, clock=lambda: now[0])

    with pytest.raises(APITransportError):
        await service.create_config(
            server_id=server.id,
            owner_id=user.id,
            name="exhausted-config",
            display_name="Exhausted",
        )
    now[0] += timedelta(seconds=6)
    assert list((await service.reconcile()).values()) == ["failed:APITransportError"]

    async with uow() as repos:
        cfg = await repos["configs"].get(name="exhausted-config")
        operation = await repos["vpn_operations"].get(operation_id=cfg.operation_id)
    assert operation.status == VPNOperationStatus.EXHAUSTED.value
    now[0] += timedelta(hours=1)
    assert await service.reconcile() == {}

    gateway.behavior = "success"
    await service.revoke_config(cfg.id)

    assert await service.get(cfg.id) is None
    assert gateway.calls[-1][0] == "revoke"


@pytest.mark.asyncio
async def test_opposite_entitlement_supersedes_ambiguous_suspend(
    monkeypatch, sessionmaker
):
    now = [datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)]
    gateway = LifecycleGateway()
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *args, **kwargs: gateway
    )
    user, server = await _user_and_server()
    service = ConfigService(uow, clock=lambda: now[0])
    cfg = await service.create_config(
        server_id=server.id,
        owner_id=user.id,
        name="latest-entitlement",
        display_name="Latest",
    )

    gateway.behavior = "transport"
    with pytest.raises(APITransportError):
        await service.suspend_config(cfg.id)
    async with uow() as repos:
        pending = await repos["configs"].get(id=cfg.id)
        suspend_id = pending.operation_id

    gateway.behavior = "success"
    await service.unsuspend_config(cfg.id)

    async with uow() as repos:
        row = await repos["configs"].get(id=cfg.id)
        old_suspend = await repos["vpn_operations"].get(operation_id=suspend_id)
        latest = await repos["vpn_operations"].get(operation_id=row.operation_id)
    assert old_suspend.status == VPNOperationStatus.SUPERSEDED.value
    assert latest.kind == VPNOperationKind.UNSUSPEND.value
    assert latest.status == VPNOperationStatus.SUCCEEDED.value
    assert row.desired_state == VPNState.ACTIVE.value
    assert row.actual_state == VPNState.ACTIVE.value


@pytest.mark.asyncio
async def test_maintenance_defers_activation_without_consuming_attempt(
    monkeypatch, sessionmaker
):
    now = [datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)]
    gateway = LifecycleGateway("transport")
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *args, **kwargs: gateway
    )
    user, server = await _user_and_server()
    service = ConfigService(uow, clock=lambda: now[0])
    with pytest.raises(APITransportError):
        await service.create_config(
            server_id=server.id,
            owner_id=user.id,
            name="maintenance-pending",
            display_name="Pending",
        )
    now[0] += timedelta(seconds=6)

    monkeypatch.setattr(settings, "maintenance_mode", True)
    assert await service.reconcile() == {}
    async with uow() as repos:
        cfg = await repos["configs"].get(name="maintenance-pending")
        deferred = await repos["vpn_operations"].get(operation_id=cfg.operation_id)
    assert deferred.attempts == 1

    monkeypatch.setattr(settings, "maintenance_mode", False)
    gateway.behavior = "success"
    assert list((await service.reconcile()).values()) == [
        VPNOperationStatus.SUCCEEDED.value
    ]


@pytest.mark.asyncio
async def test_terminal_operations_do_not_starve_due_work(sessionmaker):
    now = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    user, server = await _user_and_server()
    async with uow() as repos:
        for index in range(105):
            repos["vpn_operations"].session.add(
                repos["vpn_operations"].model(
                    operation_id=f"00000000-0000-4000-8001-{index:012d}",
                    config_id=None,
                    config_name=f"terminal-{index}",
                    server_id=server.id,
                    owner_id=user.id,
                    kind=VPNOperationKind.PROVISION.value,
                    status=VPNOperationStatus.REJECTED.value,
                    next_attempt_at=now - timedelta(days=1),
                )
            )
        due = await repos["vpn_operations"].create(
            operation_id="00000000-0000-4000-8002-000000000001",
            config_id=None,
            config_name="due",
            server_id=server.id,
            owner_id=user.id,
            kind=VPNOperationKind.SUSPEND.value,
            next_attempt_at=now,
        )

    async with uow() as repos:
        page = await repos["vpn_operations"].list_due(now=now, limit=1)
    assert [operation.operation_id for operation in page] == [due.operation_id]


@pytest.mark.asyncio
async def test_batch_publishes_all_intents_before_first_remote_call(
    monkeypatch, sessionmaker
):
    gateway = LifecycleGateway()
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *args, **kwargs: gateway
    )
    user, server = await _user_and_server()
    service = ConfigService(uow)
    first = await service.create_config(
        server_id=server.id,
        owner_id=user.id,
        name="batch-first",
        display_name="First",
    )
    second = await service.create_config(
        server_id=server.id,
        owner_id=user.id,
        name="batch-second",
        display_name="Second",
    )

    observed: list[tuple[list[str], list[str]]] = []
    original_suspend = gateway.suspend_client

    async def inspect_then_suspend(name, *, operation_id=None):
        if not observed:
            async with uow() as repos:
                rows = await repos["configs"].list(owner_id=user.id)
                operations = [
                    await repos["vpn_operations"].get(operation_id=row.operation_id)
                    for row in rows
                ]
            observed.append(
                (
                    [row.desired_state for row in rows],
                    [operation.kind for operation in operations],
                )
            )
        await original_suspend(name, operation_id=operation_id)

    gateway.suspend_client = inspect_then_suspend
    assert await service.suspend_all(user.id) == 2
    assert observed == (
        [
            (
                [VPNState.SUSPENDED.value, VPNState.SUSPENDED.value],
                [VPNOperationKind.SUSPEND.value, VPNOperationKind.SUSPEND.value],
            )
        ]
    )
    async with uow() as repos:
        rows = await repos["configs"].list(owner_id=user.id)
    assert {row.id for row in rows} == {first.id, second.id}
    assert all(row.actual_state == VPNState.SUSPENDED.value for row in rows)
