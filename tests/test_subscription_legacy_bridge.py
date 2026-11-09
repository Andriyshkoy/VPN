from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import select, update

from core.config import settings
from core.db.models.billing_run import BillingRun
from core.db.models.ledger import LedgerKind
from core.db.models.user import User
from core.db.unit_of_work import uow
from core.exceptions import APIRequestRejectedError, InvalidOperationError
from core.services import BillingService, ConfigService, ServerService, UserService
from tests.fleet_test_support import mark_server_ready
from tests.test_subscription_service import product


class Gateway:
    def __init__(self, *, reject: bool = False) -> None:
        self.reject = reject
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def create_client(self, *args, **kwargs):
        self.calls += 1
        if self.reject:
            raise APIRequestRejectedError("rejected", status_code=422)

    async def suspend_client(self, *args, **kwargs):
        return None


async def server_and_gateway(monkeypatch, *, reject: bool = False):
    server = await ServerService(uow).create(
        name="legacy-bridge",
        ip="127.0.0.1",
        port=8080,
        host="vpn.test",
        location="test",
        api_key="secret",
        cost=0,
    )
    await mark_server_ready(server.id)
    gateway = Gateway(reject=reject)
    monkeypatch.setattr("core.services.config.APIGateway", lambda *a, **k: gateway)
    return server, gateway


@pytest.mark.asyncio
async def test_legacy_period_excludes_product_and_unlimited_accounts(
    sessionmaker, monkeypatch
):
    server, _ = await server_and_gateway(monkeypatch)
    users = [
        await UserService(uow).register(tg_id, balance="20.00")
        for tg_id in (71001, 71002, 71003)
    ]
    for user in users:
        await ConfigService(uow).create_config(
            server_id=server.id,
            owner_id=user.id,
            name=f"legacy-{user.id}",
            display_name="Legacy",
        )
    async with uow() as repos:
        # Even a fully suspended product record owns billing, regardless of
        # technical config drift. The old hourly job must never take money.
        await repos["subscriptions"].create(users[0].id, "basic", status="suspended")
        await repos["users"].update(users[1].id, unlimited_balance=True)
    now = datetime(2026, 11, 9, tzinfo=timezone.utc)
    async with uow() as repos:
        results = await repos["billing"].charge_period(
            period_key="legacy-bridge",
            period_start=now,
            period_end=now + timedelta(hours=1),
            cost_per_config="1.00",
        )
    assert [(row.user.id, row.amount) for row in results] == [
        (users[2].id, Decimal("1.00"))
    ]
    assert [(await UserService(uow).get(user.id)).balance for user in users] == [
        Decimal("20.00"),
        Decimal("20.00"),
        Decimal("19.00"),
    ]


@pytest.mark.asyncio
async def test_unlimited_waives_consumption_preserves_history_and_manual_wallet(
    sessionmaker,
):
    user = await UserService(uow).register(71004, balance="20.00")
    async with uow() as repos:
        old = await repos["billing"].apply_balance_change(
            user_id=user.id,
            amount="-5.00",
            kind=LedgerKind.PERIODIC_CHARGE,
            idempotency_key="prior-debit",
            allow_negative_balance=False,
        )
        old_id = old.ledger_entry.id
        await repos["users"].update(user.id, unlimited_balance=True)
    async with uow() as repos:
        historical = await repos["billing"].apply_balance_change(
            user_id=user.id,
            amount="-5.00",
            kind=LedgerKind.PERIODIC_CHARGE,
            idempotency_key="prior-debit",
            allow_negative_balance=False,
        )
        assert historical.ledger_entry.id == old_id
        assert historical.applied is False
        for kind in (LedgerKind.PERIODIC_CHARGE, LedgerKind.CONFIG_RESERVATION):
            waived = await repos["billing"].apply_balance_change(
                user_id=user.id,
                amount="-100.00",
                kind=kind,
                idempotency_key=f"waived:{kind}",
                allow_negative_balance=False,
            )
            assert waived.applied is False
            assert waived.ledger_entry is None
            assert waived.user.balance == Decimal("15.00")
        manual = await repos["billing"].apply_balance_change(
            user_id=user.id,
            amount="-3.00",
            kind=LedgerKind.MANUAL_WITHDRAWAL,
            idempotency_key="explicit-manual",
            allow_negative_balance=False,
        )
        assert manual.applied is True
        assert manual.user.balance == Decimal("12.00")
        assert len(await repos["billing"].list_ledger_entries(user.id)) == 3
        await repos["users"].update(user.id, unlimited_balance=False)
    assert (await UserService(uow).get(user.id)).balance == Decimal("12.00")


@pytest.mark.asyncio
async def test_new_legacy_entrypoint_rejects_sales_at_zero_balance(
    sessionmaker, monkeypatch
):
    user = await UserService(uow).register(71005)
    server, gateway = await server_and_gateway(monkeypatch)
    billing = BillingService(uow, per_config_cost="1.00")
    kwargs = dict(
        server_id=server.id,
        owner_id=user.id,
        name="free-issue",
        display_name="Phone",
        creation_cost="10.00",
        idempotency_key="free",
    )
    with pytest.raises(InvalidOperationError, match="select a subscription"):
        await billing.create_paid_config(**kwargs)
    with pytest.raises(InvalidOperationError, match="select a subscription"):
        await billing.create_paid_config(**kwargs)
    assert gateway.calls == 0
    assert (await UserService(uow).get(user.id)).balance == Decimal("0.00")
    async with uow() as repos:
        assert await repos["billing"].list_ledger_entries(user.id) == []
        assert await repos["configs"].get(name="free-issue") is None
        assert await repos["vpn_operations"].list() == []


@pytest.mark.asyncio
async def test_retired_legacy_route_cannot_charge_or_manufacture_refund(
    sessionmaker, monkeypatch
):
    user = await UserService(uow).register(71006, balance="20.00")
    server, gateway = await server_and_gateway(monkeypatch, reject=True)
    billing = BillingService(uow, per_config_cost="1.00")
    with pytest.raises(InvalidOperationError, match="select a subscription"):
        await billing.create_paid_config(
            server_id=server.id,
            owner_id=user.id,
            name="free-rejected",
            display_name="Phone",
            creation_cost="10.00",
        )
    async with uow() as repos:
        assert len(await repos["billing"].list_ledger_entries(user.id)) == 1
        assert await repos["billing"].refund_rejected_config_reservations() == 0
    assert gateway.calls == 0
    assert (await UserService(uow).get(user.id)).balance == Decimal("20.00")


@pytest.mark.asyncio
async def test_historical_paid_replay_preserves_original_reservation(
    sessionmaker, monkeypatch
):
    user = await UserService(uow).register(71007, balance="20.00")
    server, gateway = await server_and_gateway(monkeypatch)
    billing = BillingService(uow, per_config_cost="1.00")
    operation_id = str(uuid5(NAMESPACE_URL, f"vpn-paid-config:{user.id}:historical"))
    async with uow() as repos:
        context = await billing._config_service.prepare_config(
            repos=repos,
            operation_id=operation_id,
            server_id=server.id,
            owner_id=user.id,
            name="historical",
            display_name="Phone",
            operation_payload={
                "paid_request_version": 1,
                "creation_cost": "10.00",
                "display_name": "Phone",
            },
        )
        await repos["billing"].apply_balance_change(
            user_id=user.id,
            amount="-10.00",
            kind=LedgerKind.CONFIG_RESERVATION,
            idempotency_key=f"config-reservation:{operation_id}",
            allow_negative_balance=False,
            reference_type="vpn_operation",
            reference_id=operation_id,
        )
    await billing._config_service.execute_prepared(context)
    replay = await billing.create_paid_config(
        server_id=server.id,
        owner_id=user.id,
        name="historical",
        display_name="Phone",
        creation_cost="10.00",
        idempotency_key="historical",
    )
    assert replay.id == context.config_id
    assert gateway.calls == 1
    assert (await UserService(uow).get(user.id)).balance == Decimal("10.00")
    async with uow() as repos:
        assert len(await repos["billing"].list_ledger_entries(user.id)) == 2


@pytest.mark.asyncio
async def test_wallet_changes_do_not_override_suspended_product_entitlements(
    sessionmaker, monkeypatch
):
    user = await UserService(uow).register(71008)
    async with uow() as repos:
        await repos["subscriptions"].create(user.id, "basic", status="suspended")
    billing = BillingService(uow, per_config_cost="1.00")

    async def forbidden(**kwargs):
        pytest.fail(
            "Wallet updates must not resume/suspend all product and old configs"
        )

    monkeypatch.setattr(billing._config_service, "prepare_entitlement", forbidden)
    await billing.top_up(user.id, "5.00")
    await billing.withdraw(user.id, "5.00")
    async with uow() as repos:
        assert (await repos["subscriptions"].get_by_user(user.id)).status == "suspended"


@pytest.mark.asyncio
async def test_hourly_tariffs_share_historical_run_and_ignore_key_count(
    sessionmaker, monkeypatch
):
    server, _ = await server_and_gateway(monkeypatch)
    basic, plus, legacy, unlimited = [
        await UserService(uow).register(tg_id, balance="1.00")
        for tg_id in (71009, 71010, 71011, 71012)
    ]
    for owner, count in ((basic, 2), (plus, 4), (legacy, 2), (unlimited, 3)):
        for index in range(count):
            await ConfigService(uow).create_config(
                server_id=server.id,
                owner_id=owner.id,
                name=f"tariff-{owner.id}-{index}",
                display_name="Existing credential",
            )
    async with uow() as repos:
        await repos["subscriptions"].create(basic.id, "basic", status="active")
        await repos["subscriptions"].create(plus.id, "plus", status="active")
        await repos["subscriptions"].create(unlimited.id, "plus", status="active")
        await repos["users"].update(unlimited.id, unlimited_balance=True)
    now = datetime(2026, 11, 9, tzinfo=timezone.utc)
    kwargs = dict(
        period_key="v1:3600:1794182400",
        period_start=now,
        period_end=now + timedelta(hours=1),
        cost_per_config="0.07",
    )
    async with uow() as repos:
        first = await repos["billing"].charge_period(**kwargs)
    assert {row.user.id: row.amount for row in first} == {
        basic.id: Decimal("0.07"),
        plus.id: Decimal("0.14"),
        legacy.id: Decimal("0.14"),
    }
    async with uow() as repos:
        assert await repos["billing"].charge_period(**kwargs) == []
        for owner in (basic, plus):
            entries = await repos["billing"].list_ledger_entries(owner.id)
            charge = entries[0]
            assert charge.kind == LedgerKind.PERIODIC_CHARGE.value
            assert (
                charge.idempotency_key
                == f"billing:{kwargs['period_key']}:user:{owner.id}"
            )
            assert charge.reference_type == "billing_run"
            assert charge.details["billing_basis"] == "account_tariff"
            assert charge.details["pricing_model"] == "subscription_hourly"
            assert len(entries) == 2
        assert len(await repos["billing"].list_ledger_entries(unlimited.id)) == 1
    assert (await UserService(uow).get(unlimited.id)).balance == Decimal("1.00")


@pytest.mark.asyncio
async def test_billing_disabled_preserves_product_wallet_and_status(
    sessionmaker, monkeypatch
):
    user = await UserService(uow).register(71013, balance="1.00")
    async with uow() as repos:
        await repos["subscriptions"].create(user.id, "plus", status="active")
    monkeypatch.setattr(settings, "billing_enabled", False)
    assert await BillingService(uow, per_config_cost="0.07").charge_all() == {}
    async with uow() as repos:
        assert len(await repos["billing"].list_ledger_entries(user.id)) == 1
        assert (await repos["subscriptions"].get_by_user(user.id)).status == "active"
    assert (await UserService(uow).get(user.id)).balance == Decimal("1.00")


@pytest.mark.asyncio
async def test_exhaustion_and_topup_keep_legacy_keys_paused(sessionmaker, monkeypatch):
    user = await UserService(uow).register(71014, balance="0.05")
    server, _ = await server_and_gateway(monkeypatch)
    old = await ConfigService(uow).create_config(
        server_id=server.id,
        owner_id=user.id,
        name="unmapped-old-key",
        display_name="Retained for rollback",
    )
    async with uow() as repos:
        await repos["subscriptions"].create(user.id, "basic", status="active")
    monkeypatch.setattr(settings, "billing_enabled", True)
    billing = BillingService(uow, per_config_cost="0.07")
    charges = await billing.charge_all(at=datetime(2026, 11, 9, tzinfo=timezone.utc))
    assert list(charges.values()) == [Decimal("0.07")]
    async with uow() as repos:
        assert (await repos["subscriptions"].get_by_user(user.id)).status == "expired"
        assert (await repos["configs"].get(id=old.id)).desired_state == "suspended"
    await billing.top_up(user.id, "1.00", idempotency_key="expired-topup")
    async with uow() as repos:
        assert (await repos["subscriptions"].get_by_user(user.id)).status == "pending"
        assert (await repos["configs"].get(id=old.id)).desired_state == "suspended"
    assert (
        await billing.charge_all(at=datetime(2026, 11, 9, 1, tzinfo=timezone.utc)) == {}
    )
    assert (await UserService(uow).get(user.id)).balance == Decimal("0.98")


@pytest.mark.asyncio
async def test_product_tariff_rejects_changed_billing_interval_before_claim(
    sessionmaker,
):
    user = await UserService(uow).register(71015, balance="1.00")
    now = datetime(2026, 11, 9, tzinfo=timezone.utc)
    async with uow() as repos:
        await repos["subscriptions"].create(user.id, "basic", status="active")
        with pytest.raises(InvalidOperationError, match="3600-second"):
            await repos["billing"].charge_period(
                period_key="v1:1800:changed",
                period_start=now,
                period_end=now + timedelta(minutes=30),
                cost_per_config="0.07",
            )
        # Catching the validation error cannot accidentally commit a claimed
        # period or any debit. A corrected hourly scheduler may retry safely.
        assert await repos["billing"].session.scalar(select(BillingRun.id)) is None
        assert len(await repos["billing"].list_ledger_entries(user.id)) == 1
    assert (await UserService(uow).get(user.id)).balance == Decimal("1.00")


@pytest.mark.asyncio
async def test_locked_debit_refreshes_unlimited_mode_from_database(sessionmaker):
    user = await UserService(uow).register(71016, balance="1.00")
    async with uow() as repos:
        session = repos["billing"].session
        cached = await session.get(User, user.id)
        assert cached.unlimited_balance is False
        await session.execute(
            update(User)
            .where(User.id == user.id)
            .values(unlimited_balance=True)
            .execution_options(synchronize_session=False)
        )
        assert cached.unlimited_balance is False
        result = await repos["billing"].apply_balance_change(
            user_id=user.id,
            amount="-0.07",
            kind=LedgerKind.PERIODIC_CHARGE,
            idempotency_key="stale-flag-debit",
            allow_negative_balance=True,
        )
        assert result.applied is False
        assert result.ledger_entry is None
        assert result.user.unlimited_balance is True
        assert result.user.balance == Decimal("1.00")


@pytest.mark.asyncio
@pytest.mark.parametrize("migration_source", [None, "legacy-active-v1"])
async def test_pending_cutover_blocks_all_billing_before_claim(
    sessionmaker, migration_source
):
    pending = await UserService(uow).register(71017, balance="1.00")
    active = await UserService(uow).register(71018, balance="1.00")
    now = datetime(2026, 11, 9, tzinfo=timezone.utc)
    kwargs = dict(
        period_key="v1:3600:pending-cutover",
        period_start=now,
        period_end=now + timedelta(hours=1),
        cost_per_config="0.07",
    )
    async with uow() as repos:
        row = await repos["subscriptions"].create(
            pending.id,
            "plus",
            status="pending",
            migration_source=migration_source,
            migration_snapshot={"cutover_state": "pending"},
        )
        await repos["subscriptions"].create(active.id, "basic", status="active")
        with pytest.raises(InvalidOperationError, match="cutover is pending"):
            await repos["billing"].charge_period(**kwargs)
        assert await repos["billing"].session.scalar(select(BillingRun.id)) is None
        for owner in (pending, active):
            assert len(await repos["billing"].list_ledger_entries(owner.id)) == 1
        # The fully paused account stays off and does not block a legitimate
        # hourly run. It must never receive the legacy fallback charge.
        await repos["subscriptions"].update(row.id, status="suspended")
        charged = await repos["billing"].charge_period(**kwargs)
        assert [(item.user.id, item.amount) for item in charged] == [
            (active.id, Decimal("0.07"))
        ]
    assert (await UserService(uow).get(pending.id)).balance == Decimal("1.00")


@pytest.mark.asyncio
async def test_completed_cutover_marker_does_not_block_hourly_billing(sessionmaker):
    user = await UserService(uow).register(71019, balance="1.00")
    now = datetime(2026, 11, 9, tzinfo=timezone.utc)
    async with uow() as repos:
        await repos["subscriptions"].create(
            user.id,
            "basic",
            status="active",
            migration_snapshot={"cutover_state": "complete"},
        )
        charged = await repos["billing"].charge_period(
            period_key="v1:3600:complete-cutover",
            period_start=now,
            period_end=now + timedelta(hours=1),
            cost_per_config="0.07",
        )
        assert [(item.user.id, item.amount) for item in charged] == [
            (user.id, Decimal("0.07"))
        ]


@pytest.mark.asyncio
async def test_positive_topup_restores_both_mapped_protocols_and_finalizes_status(
    sessionmaker, monkeypatch
):
    node, user, server, service = await product(monkeypatch)
    await service.select_plan(user.id, "plus")
    granted = await service.issue(user.id, server.id)
    remote_ids = {access.remote_access_id for access in granted.accesses}
    billing = BillingService(uow, per_config_cost="0.07")
    await billing.top_up(user.id, "0.10", idempotency_key="mapped-small-topup")
    monkeypatch.setattr(settings, "billing_enabled", True)
    now = datetime(2026, 11, 9, tzinfo=timezone.utc)
    charges = await billing.charge_all(at=now)
    assert list(charges.values()) == [Decimal("0.14")]
    assert (await service.get_current(user.id)).status == "expired"
    assert all(node.accesses[key]["status"] == "suspended" for key in remote_ids)
    node.calls.clear()
    await billing.top_up(user.id, "1.00", idempotency_key="mapped-full-topup")
    restored = await service.get_current(user.id)
    assert restored.status == "active"
    assert {access.remote_access_id for access in restored.accesses} == remote_ids
    assert all(node.accesses[key]["status"] == "active" for key in remote_ids)
    assert [call[0] for call in node.calls] == ["active", "active"]
    assert (await service.export(user.id, "hysteria")).uri
    assert (await UserService(uow).get(user.id)).balance == Decimal("0.96")


@pytest.mark.asyncio
async def test_reconcile_repairs_pending_status_after_remote_operation_succeeded(
    sessionmaker, monkeypatch
):
    node, user, server, service = await product(monkeypatch)
    await service.select_plan(user.id, "plus")
    granted = await service.issue(user.id, server.id)
    async with uow() as repos:
        # Simulate a transient failure of the final product transaction after
        # both durable remote operations already reached SUCCEEDED.
        await repos["subscriptions"].update(
            granted.id,
            status="pending",
            migration_snapshot={"cutover_state": "pending"},
        )
    node.calls.clear()
    result = await BillingService(
        uow, per_config_cost="0.07"
    ).reconcile_pending_config_operations()
    assert result == ({}, 0)
    recovered = await service.get_current(user.id)
    assert recovered.status == "active"
    assert recovered.accesses == granted.accesses
    assert node.calls == []
    async with uow() as repos:
        assert await repos["billing"].list_ledger_entries(user.id) == []
        row = await repos["subscriptions"].get_by_user(user.id)
        assert row.migration_snapshot["cutover_state"] == "complete"
