from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.config import settings
from core.db.unit_of_work import uow
from core.domain import VPNOperationStatus, VPNState
from core.exceptions import (
    APIHTTPError,
    APINotFoundError,
    APITLSConfigurationError,
    APITransportError,
)
from core.services import BillingService, ConfigService, ServerService, UserService


class LifecycleGateway:
    def __init__(self, behavior: str = "success"):
        self.behavior = behavior
        self.operation_ids: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def create_client(self, name, use_password=False, *, operation_id=None):
        self.operation_ids.append(operation_id)
        if self.behavior in {"transport", "transport-after-effect"}:
            raise APITransportError("timeout")
        if self.behavior == "rejected":
            raise APIHTTPError("bad request", status_code=400)
        return "/tmp/client.ovpn"

    async def download_config(self, name):
        if self.behavior == "transport":
            raise APINotFoundError("missing", status_code=404)
        return b"config"

    async def suspend_client(self, name, *, operation_id=None):
        self.operation_ids.append(operation_id)
        if self.behavior == "transport":
            raise APITransportError("timeout")

    async def unsuspend_client(self, name, *, operation_id=None):
        self.operation_ids.append(operation_id)
        if self.behavior == "transport":
            raise APITransportError("timeout")

    async def revoke_client(self, name, *, operation_id=None):
        self.operation_ids.append(operation_id)
        if self.behavior == "not-found":
            raise APINotFoundError("gone", status_code=404)

    async def list_blocked(self):
        return []


async def _user_and_server():
    user = await UserService(uow).register(912345)
    server = await ServerService(uow).create(
        name="lifecycle",
        ip="127.0.0.1",
        port=8080,
        host="vpn.test",
        location="local",
        api_key="secret",
        cost=0,
    )
    return user, server


@pytest.mark.asyncio
async def test_provision_success_records_durable_operation(monkeypatch, sessionmaker):
    monkeypatch.setattr(settings, "maintenance_mode", False)
    monkeypatch.setattr(settings, "provisioning_enabled", True)
    gateway = LifecycleGateway()
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *args, **kwargs: gateway
    )
    user, server = await _user_and_server()

    cfg = await ConfigService(uow).create_config(
        server_id=server.id,
        owner_id=user.id,
        name="provision-success",
        display_name="My device",
    )

    async with uow() as repos:
        row = await repos["configs"].get(id=cfg.id)
        operation = await repos["vpn_operations"].get(operation_id=row.operation_id)
    assert row.actual_state == VPNState.ACTIVE.value
    assert row.desired_state == VPNState.ACTIVE.value
    assert operation.status == VPNOperationStatus.SUCCEEDED.value
    assert operation.attempts == 1
    assert gateway.operation_ids == [row.operation_id]


@pytest.mark.asyncio
async def test_ambiguous_create_is_verified_by_download(monkeypatch, sessionmaker):
    gateway = LifecycleGateway("transport-after-effect")
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *args, **kwargs: gateway
    )
    user, server = await _user_and_server()

    cfg = await ConfigService(uow).create_config(
        server_id=server.id,
        owner_id=user.id,
        name="verified-after-timeout",
        display_name="Verified",
    )

    async with uow() as repos:
        row = await repos["configs"].get(id=cfg.id)
        operation = await repos["vpn_operations"].get(operation_id=row.operation_id)
    assert row.actual_state == VPNState.ACTIVE.value
    assert operation.status == VPNOperationStatus.SUCCEEDED.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("behavior", "actual_state", "operation_status"),
    [
        (
            "transport",
            VPNState.PROVISIONING.value,
            VPNOperationStatus.FAILED.value,
        ),
        ("rejected", VPNState.FAILED.value, VPNOperationStatus.REJECTED.value),
    ],
)
async def test_provision_failure_is_recoverable(
    monkeypatch,
    sessionmaker,
    behavior,
    actual_state,
    operation_status,
):
    gateway = LifecycleGateway(behavior)
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *args, **kwargs: gateway
    )
    user, server = await _user_and_server()
    service = ConfigService(uow)

    with pytest.raises((APITransportError, APIHTTPError)):
        await service.create_config(
            server_id=server.id,
            owner_id=user.id,
            name=f"provision-{behavior}",
            display_name="My device",
        )

    async with uow() as repos:
        row = await repos["configs"].get(name=f"provision-{behavior}")
        operation = await repos["vpn_operations"].get(operation_id=row.operation_id)
    assert row.desired_state == VPNState.ACTIVE.value
    assert row.actual_state == actual_state
    assert row.last_error
    assert operation.status == operation_status
    presented = await service.get(row.id)
    assert presented.operation_status == operation_status
    assert presented.operation_attempts == operation.attempts
    listed = await service.list(owner_id=user.id)
    assert listed[0].operation_status == operation_status


@pytest.mark.asyncio
async def test_tls_material_failure_keeps_operation_retryable_and_not_refunded(
    monkeypatch,
    sessionmaker,
):
    def missing_tls_gateway(*args, **kwargs):
        raise APITLSConfigurationError("TLS secret mount is temporarily missing")

    monkeypatch.setattr("core.services.config.APIGateway", missing_tls_gateway)
    user, server = await _user_and_server()
    billing = BillingService(uow, per_config_cost="1.00")
    await billing.top_up(user.id, "10.00", idempotency_key="tls-retry-seed")

    with pytest.raises(APITLSConfigurationError):
        await billing.create_paid_config(
            server_id=server.id,
            owner_id=user.id,
            name="tls-retryable",
            display_name="TLS retryable",
            creation_cost="10.00",
        )

    async with uow() as repos:
        config = await repos["configs"].get(name="tls-retryable")
        operation = await repos["vpn_operations"].get(operation_id=config.operation_id)
    assert operation.status == VPNOperationStatus.FAILED.value
    assert operation.next_attempt_at is not None
    assert config.actual_state == VPNState.PROVISIONING.value
    _, refunded = await billing.reconcile_pending_config_operations()
    assert refunded == 0
    assert (await UserService(uow).get(user.id)).balance == Decimal("0.00")


@pytest.mark.asyncio
async def test_rejected_provision_can_be_cleaned_up_without_remote_revoke(
    monkeypatch, sessionmaker
):
    gateway = LifecycleGateway("rejected")
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *args, **kwargs: gateway
    )
    user, server = await _user_and_server()
    service = ConfigService(uow)

    with pytest.raises(APIHTTPError):
        await service.create_config(
            server_id=server.id,
            owner_id=user.id,
            name="rejected-cleanup",
            display_name="Rejected cleanup",
        )
    async with uow() as repos:
        cfg = await repos["configs"].get(name="rejected-cleanup")
    calls_before_cleanup = list(gateway.operation_ids)

    await service.revoke_config(cfg.id)

    assert await service.get(cfg.id) is None
    assert gateway.operation_ids == calls_before_cleanup


@pytest.mark.asyncio
async def test_reconcile_retries_ambiguous_operation_with_same_id(
    monkeypatch, sessionmaker
):
    gateway = LifecycleGateway("transport")
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *args, **kwargs: gateway
    )
    user, server = await _user_and_server()
    clock = [datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)]
    service = ConfigService(uow, clock=lambda: clock[0])

    with pytest.raises(APITransportError):
        await service.create_config(
            server_id=server.id,
            owner_id=user.id,
            name="retryable-provision",
            display_name="My device",
        )
    first_operation_id = gateway.operation_ids[-1]

    gateway.behavior = "success"
    clock[0] += timedelta(seconds=6)
    result = await service.reconcile()

    assert list(result.values()) == [VPNOperationStatus.SUCCEEDED.value]
    async with uow() as repos:
        row = await repos["configs"].get(name="retryable-provision")
        operation = await repos["vpn_operations"].get(operation_id=row.operation_id)
    assert row.actual_state == VPNState.ACTIVE.value
    assert operation.attempts == 2
    assert gateway.operation_ids[-1] == first_operation_id


@pytest.mark.asyncio
async def test_latest_entitlement_waits_for_ambiguous_provision_then_converges(
    monkeypatch, sessionmaker
):
    gateway = LifecycleGateway("transport")
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *args, **kwargs: gateway
    )
    user, server = await _user_and_server()
    clock = [datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)]
    service = ConfigService(uow, clock=lambda: clock[0])

    with pytest.raises(APITransportError):
        await service.create_config(
            server_id=server.id,
            owner_id=user.id,
            name="pending-transition",
            display_name="My device",
        )
    async with uow() as repos:
        cfg = await repos["configs"].get(name="pending-transition")

    await service.suspend_config(cfg.id)
    async with uow() as repos:
        pending = await repos["configs"].get(id=cfg.id)
        provision = await repos["vpn_operations"].get(operation_id=pending.operation_id)
    assert pending.desired_state == VPNState.SUSPENDED.value
    assert pending.actual_state == VPNState.PROVISIONING.value
    assert provision.status == VPNOperationStatus.FAILED.value

    gateway.behavior = "success"
    clock[0] += timedelta(seconds=6)
    assert list((await service.reconcile()).values()) == [
        VPNOperationStatus.SUCCEEDED.value
    ]
    async with uow() as repos:
        converged = await repos["configs"].get(id=cfg.id)
        operations = await repos["vpn_operations"].list()
    assert converged.desired_state == VPNState.SUSPENDED.value
    assert converged.actual_state == VPNState.SUSPENDED.value
    assert [operation.status for operation in operations] == [
        VPNOperationStatus.SUCCEEDED.value,
        VPNOperationStatus.SUCCEEDED.value,
    ]


@pytest.mark.asyncio
async def test_revoke_not_found_is_idempotent(monkeypatch, sessionmaker):
    gateway = LifecycleGateway()
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *args, **kwargs: gateway
    )
    user, server = await _user_and_server()
    service = ConfigService(uow)
    cfg = await service.create_config(
        server_id=server.id,
        owner_id=user.id,
        name="already-revoked",
        display_name="My device",
    )

    gateway.behavior = "not-found"
    await service.revoke_config(cfg.id)

    assert await service.get(cfg.id) is None
    async with uow() as repos:
        operations = await repos["vpn_operations"].list()
    assert operations[-1].status == VPNOperationStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_reconciliation_refunds_later_definitive_rejection(
    monkeypatch, sessionmaker
):
    gateway = LifecycleGateway("transport")
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *args, **kwargs: gateway
    )
    user, server = await _user_and_server()
    billing = BillingService(uow, per_config_cost="1.00")
    clock = [datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)]
    billing._config_service = ConfigService(uow, clock=lambda: clock[0])
    await billing.top_up(user.id, "10.00", idempotency_key="seed-reconcile")

    with pytest.raises(APITransportError):
        await billing.create_paid_config(
            server_id=server.id,
            owner_id=user.id,
            name="refund-after-reconcile",
            display_name="My device",
            creation_cost="10.00",
        )
    assert (await UserService(uow).get(user.id)).balance == Decimal("0.00")

    gateway.behavior = "rejected"
    clock[0] += timedelta(seconds=6)
    _, refunded = await billing.reconcile_pending_config_operations()
    _, duplicate_refund = await billing.reconcile_pending_config_operations()

    assert refunded == 1
    assert duplicate_refund == 0
    assert (await UserService(uow).get(user.id)).balance == Decimal("10.00")
