from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from core.config import settings
from core.db.models.ledger import LedgerKind
from core.db.models.notification_outbox import NotificationOutbox
from core.db.models.payment import ProviderPayment
from core.db.repo.billing import BillingRepo, to_money
from core.db.unit_of_work import uow
from core.domain import VPNOperationStatus, VPNState
from core.exceptions import (
    APINotFoundError,
    APIRequestRejectedError,
    APITransportError,
    InsufficientBalanceError,
    InvalidOperationError,
)
from core.services import BillingService, ServerService, UserService


class BillingGateway:
    def __init__(self, behavior: str = "success") -> None:
        self.behavior = behavior
        self.create_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def create_client(self, *args, **kwargs):
        self.create_calls += 1
        self._mutate()

    async def download_config(self, *args, **kwargs):
        raise APINotFoundError("missing", status_code=404)

    async def suspend_client(self, *args, **kwargs):
        self._mutate()

    def _mutate(self) -> None:
        if self.behavior == "rejected":
            raise APIRequestRejectedError("bad request", status_code=400)
        if self.behavior == "transport":
            raise APITransportError("timeout")


async def _user_and_server(*, balance: Decimal = Decimal("0.00")):
    user = await UserService(uow).register(7654321, balance=balance)
    server = await ServerService(uow).create(
        name="billing-test",
        ip="127.0.0.1",
        port=8080,
        host="vpn.test",
        location="local",
        api_key="secret",
        cost=0,
    )
    return user, server


@pytest.mark.asyncio
async def test_money_conversion_does_not_preserve_float_artifacts(sessionmaker):
    assert to_money(0.1) == Decimal("0.10")
    assert to_money("1.005") == Decimal("1.01")
    with pytest.raises(InvalidOperationError):
        to_money("NaN")


@pytest.mark.asyncio
async def test_top_up_is_idempotent_and_writes_one_ledger_entry(sessionmaker):
    user = await UserService(uow).register(101)
    billing = BillingService(uow, per_config_cost="1.00")

    first = await billing.top_up(user.id, 0.1, idempotency_key="payment:test:1")
    second = await billing.top_up(user.id, 0.1, idempotency_key="payment:test:1")

    assert first.balance == Decimal("0.10")
    assert second.balance == Decimal("0.10")
    async with uow() as repos:
        entries = await BillingRepo(repos["users"].session).list_ledger_entries(user.id)
    assert len(entries) == 1
    assert entries[0].kind == LedgerKind.MANUAL_TOP_UP.value
    assert entries[0].amount == Decimal("0.10")
    assert entries[0].balance_after == Decimal("0.10")


@pytest.mark.asyncio
async def test_registration_balance_is_ledgered_only_for_new_user(sessionmaker):
    service = UserService(uow)
    created = await service.register(105, balance="7.50")
    duplicate = await service.register(105, balance="99.00")

    assert created.balance == Decimal("7.50")
    assert duplicate.balance == Decimal("7.50")
    async with uow() as repos:
        entries = await repos["billing"].list_ledger_entries(created.id)
    assert len(entries) == 1
    assert entries[0].kind == LedgerKind.OPENING_BALANCE.value
    assert entries[0].amount == Decimal("7.50")


@pytest.mark.asyncio
async def test_withdraw_accepts_exact_balance_and_never_overdraws(sessionmaker):
    user = await UserService(uow).register(102)
    billing = BillingService(uow, per_config_cost="1.00")
    await billing.top_up(user.id, "10.00", idempotency_key="seed:102")

    emptied = await billing.withdraw(
        user.id, Decimal("10.00"), idempotency_key="withdraw:102"
    )
    assert emptied.balance == Decimal("0.00")

    with pytest.raises(InsufficientBalanceError):
        await billing.withdraw(
            user.id, Decimal("0.01"), idempotency_key="withdraw:102:too-much"
        )
    assert (await UserService(uow).get(user.id)).balance == Decimal("0.00")


@pytest.mark.asyncio
async def test_balance_and_suspend_intent_commit_before_manager_execution(
    monkeypatch, sessionmaker
):
    user, server = await _user_and_server(balance=Decimal("1.00"))
    async with uow() as repos:
        cfg = await repos["configs"].create(
            server.id,
            user.id,
            "atomic-entitlement",
            "Atomic entitlement",
        )
    billing = BillingService(uow, per_config_cost="1.00")

    async def simulate_process_stopping_after_commit(*args, **kwargs):
        return 0

    monkeypatch.setattr(
        billing._config_service,
        "execute_operations",
        simulate_process_stopping_after_commit,
    )
    await billing.withdraw(
        user.id,
        "1.00",
        idempotency_key="atomic-entitlement:withdraw",
    )

    async with uow() as repos:
        row = await repos["configs"].get(id=cfg.id)
        operation = await repos["vpn_operations"].get(operation_id=row.operation_id)
    assert (await UserService(uow).get(user.id)).balance == Decimal("0.00")
    assert row.desired_state == VPNState.SUSPENDED.value
    assert operation.status == VPNOperationStatus.PENDING.value


@pytest.mark.asyncio
async def test_telegram_payment_is_credited_once_and_validates_intent(sessionmaker):
    user = await UserService(uow).register(103)
    billing = BillingService(uow, per_config_cost="1.00")
    intent = await billing.create_payment_intent(
        user_id=user.id, amount="12.34", currency="RUB"
    )
    validated = await billing.validate_payment_intent(
        user_id=user.id,
        payload=intent.payload,
        amount="12.34",
        currency="RUB",
    )
    assert validated == intent
    with pytest.raises(InvalidOperationError):
        await billing.validate_payment_intent(
            user_id=user.id,
            payload=intent.payload,
            amount="99.99",
            currency="RUB",
        )

    first = await billing.record_telegram_payment(
        user_id=user.id,
        telegram_payment_charge_id="tg-charge-1",
        total_amount_minor=1234,
        currency="RUB",
        payload=intent.payload,
        intent_id=intent.intent_id,
    )
    duplicate = await billing.record_telegram_payment(
        user_id=user.id,
        telegram_payment_charge_id="tg-charge-1",
        total_amount_minor=1234,
        currency="RUB",
        payload=intent.payload,
        intent_id=intent.intent_id,
    )

    assert first.credited is True
    assert duplicate.credited is False
    assert duplicate.user.balance == Decimal("12.34")
    with pytest.raises(InvalidOperationError):
        await billing.record_telegram_payment(
            user_id=user.id,
            telegram_payment_charge_id="different-charge-for-same-intent",
            total_amount_minor=1234,
            currency="RUB",
            payload=intent.payload,
            intent_id=intent.intent_id,
        )
    with pytest.raises(InvalidOperationError):
        await billing.record_telegram_payment(
            user_id=user.id,
            telegram_payment_charge_id="tg-charge-1",
            total_amount_minor=9999,
            currency="RUB",
            payload=intent.payload,
            intent_id=intent.intent_id,
        )


@pytest.mark.asyncio
async def test_payment_intent_replay_returns_one_stable_invoice(sessionmaker):
    user = await UserService(uow).register(107)
    billing = BillingService(uow, per_config_cost="1.00")

    first = await billing.create_payment_intent(
        user_id=user.id,
        amount="100.00",
        currency="RUB",
        idempotency_key="telegram:invoice:update:7001",
    )
    replay = await billing.create_payment_intent(
        user_id=user.id,
        amount="100.00",
        currency="RUB",
        idempotency_key="telegram:invoice:update:7001",
    )

    assert replay == first
    with pytest.raises(InvalidOperationError, match="does not match intent"):
        await billing.create_payment_intent(
            user_id=user.id,
            amount="200.00",
            currency="RUB",
            idempotency_key="telegram:invoice:update:7001",
        )
    async with uow() as repos:
        count = await repos["users"].session.scalar(
            select(func.count()).select_from(ProviderPayment)
        )
    assert count == 1


@pytest.mark.asyncio
async def test_payment_currency_and_intent_expiry_are_enforced(sessionmaker):
    user = await UserService(uow).register(106)
    billing = BillingService(uow, per_config_cost="1.00")

    with pytest.raises(InvalidOperationError, match="Only RUB"):
        await billing.create_payment_intent(
            user_id=user.id,
            amount="10.00",
            currency="USD",
        )

    intent = await billing.create_payment_intent(
        user_id=user.id,
        amount="10.00",
        currency="RUB",
    )
    async with uow() as repos:
        payment = await repos["users"].session.scalar(
            select(ProviderPayment).where(ProviderPayment.intent_id == intent.intent_id)
        )
        payment.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    with pytest.raises(InvalidOperationError, match="expired"):
        await billing.validate_payment_intent(
            user_id=user.id,
            payload=intent.payload,
            amount="10.00",
            currency="RUB",
        )


@pytest.mark.asyncio
async def test_periodic_charge_claims_stable_period_once(sessionmaker):
    user, server = await _user_and_server(balance=Decimal("10.00"))
    async with uow() as repos:
        await repos["configs"].create(
            server.id, user.id, "periodic-test", "Periodic test"
        )

    def clock():
        return datetime(2026, 7, 12, 12, 15, tzinfo=timezone.utc)

    billing = BillingService(
        uow, per_config_cost="2.00", billing_period_seconds=3600, clock=clock
    )
    first = await billing.charge_all()
    duplicate = await billing.charge_all()

    assert list(first.values()) == [Decimal("2.00")]
    assert duplicate == {}
    assert (await UserService(uow).get(user.id)).balance == Decimal("8.00")


@pytest.mark.asyncio
async def test_overlapping_billing_schedule_is_rejected(sessionmaker):
    user, server = await _user_and_server(balance=Decimal("10.00"))
    async with uow() as repos:
        await repos["configs"].create(
            server.id, user.id, "overlap-test", "Overlap test"
        )

    hourly = BillingService(
        uow,
        per_config_cost="1.00",
        billing_period_seconds=3600,
        clock=lambda: datetime(2026, 7, 12, 12, 15, tzinfo=timezone.utc),
    )
    half_hourly = BillingService(
        uow,
        per_config_cost="1.00",
        billing_period_seconds=1800,
        clock=lambda: datetime(2026, 7, 12, 12, 45, tzinfo=timezone.utc),
    )
    await hourly.charge_all()

    with pytest.raises(InvalidOperationError, match="overlaps"):
        await half_hourly.charge_all()


@pytest.mark.asyncio
async def test_billing_notice_is_committed_to_postgres_outbox(sessionmaker):
    user, server = await _user_and_server(balance=Decimal("25.00"))
    async with uow() as repos:
        await repos["configs"].create(server.id, user.id, "outbox-test", "Outbox test")

    billing = BillingService(
        uow,
        per_config_cost="1.00",
        billing_period_seconds=3600,
        clock=lambda: datetime(2026, 7, 12, 12, 15, tzinfo=timezone.utc),
    )
    await billing.charge_all()

    async with uow() as repos:
        item = await repos["users"].session.scalar(select(NotificationOutbox))
    assert item is not None
    assert item.status == "pending"
    assert item.chat_id == user.tg_id
    assert "сутки" in item.text


@pytest.mark.asyncio
async def test_outbox_republishes_until_consumer_settles(monkeypatch, sessionmaker):
    monkeypatch.setattr(settings, "notification_visibility_timeout", 30)
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    async with uow() as repos:
        await repos["billing"].add_notification_outbox(
            dedupe_key="outbox-recovery:1",
            chat_id=777,
            text="recover me",
        )

    async with uow() as repos:
        items = await repos["billing"].claim_notification_outbox(now=now)
        assert len(items) == 1
        await repos["billing"].mark_notification_published(items[0], now=now)

    async with uow() as repos:
        assert (
            await repos["billing"].claim_notification_outbox(
                now=now + timedelta(seconds=29)
            )
            == []
        )
        stale = await repos["billing"].claim_notification_outbox(
            now=now + timedelta(seconds=31)
        )
        assert [item.dedupe_key for item in stale] == ["outbox-recovery:1"]
        assert await repos["billing"].settle_notification_outbox(
            dedupe_key="outbox-recovery:1",
            delivered=True,
        )

    async with uow() as repos:
        assert (
            await repos["billing"].claim_notification_outbox(
                now=now + timedelta(hours=1)
            )
            == []
        )


@pytest.mark.asyncio
async def test_create_paid_config_reserves_exact_balance(monkeypatch, sessionmaker):
    user, server = await _user_and_server(balance=Decimal("10.00"))
    billing = BillingService(uow, per_config_cost="1.00")
    gateway = BillingGateway()
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *args, **kwargs: gateway
    )
    result = await billing.create_paid_config(
        server_id=server.id,
        owner_id=user.id,
        name="exact-balance",
        display_name="Exact balance",
        creation_cost="10.00",
    )
    assert result.name == "exact-balance"
    assert (await UserService(uow).get(user.id)).balance == Decimal("0.00")


@pytest.mark.asyncio
async def test_definitive_provision_failure_refunds_reservation(
    monkeypatch, sessionmaker
):
    user, server = await _user_and_server(balance=Decimal("10.00"))
    billing = BillingService(uow, per_config_cost="1.00")
    gateway = BillingGateway("rejected")
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *args, **kwargs: gateway
    )
    with pytest.raises(APIRequestRejectedError):
        await billing.create_paid_config(
            server_id=server.id,
            owner_id=user.id,
            name="rejected",
            display_name="Rejected",
            creation_cost="10.00",
        )

    assert (await UserService(uow).get(user.id)).balance == Decimal("10.00")
    async with uow() as repos:
        entries = await BillingRepo(repos["users"].session).list_ledger_entries(user.id)
    assert [entry.amount for entry in reversed(entries)] == [
        Decimal("10.00"),
        Decimal("-10.00"),
        Decimal("10.00"),
    ]


@pytest.mark.asyncio
async def test_ambiguous_provision_failure_keeps_reservation(monkeypatch, sessionmaker):
    user, server = await _user_and_server(balance=Decimal("10.00"))
    billing = BillingService(uow, per_config_cost="1.00")
    gateway = BillingGateway("transport")
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *args, **kwargs: gateway
    )
    with pytest.raises(APITransportError):
        await billing.create_paid_config(
            server_id=server.id,
            owner_id=user.id,
            name="ambiguous",
            display_name="Ambiguous",
            creation_cost="10.00",
        )

    assert (await UserService(uow).get(user.id)).balance == Decimal("0.00")
    async with uow() as repos:
        entries = await BillingRepo(repos["users"].session).list_ledger_entries(user.id)
    assert [entry.amount for entry in entries] == [
        Decimal("-10.00"),
        Decimal("10.00"),
    ]


@pytest.mark.asyncio
async def test_paid_config_replay_returns_same_config_and_never_charges_twice(
    monkeypatch, sessionmaker
):
    user, server = await _user_and_server(balance=Decimal("20.00"))
    billing = BillingService(uow, per_config_cost="1.00")
    gateway = BillingGateway()
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *args, **kwargs: gateway
    )
    result = await billing.create_paid_config(
        server_id=server.id,
        owner_id=user.id,
        name="same-purchase",
        display_name="First attempt",
        creation_cost="10.00",
        idempotency_key="client-request-1",
    )
    assert result.name == "same-purchase"

    replay = await billing.create_paid_config(
        server_id=server.id,
        owner_id=user.id,
        name="same-purchase",
        display_name="First attempt",
        creation_cost="10.00",
        idempotency_key="client-request-1",
    )
    assert replay.id == result.id
    assert gateway.create_calls == 1

    with pytest.raises(InvalidOperationError, match="another VPN purchase"):
        await billing.create_paid_config(
            server_id=server.id,
            owner_id=user.id,
            name="same-purchase",
            display_name="Duplicate attempt",
            creation_cost="10.00",
            idempotency_key="client-request-1",
        )

    assert (await UserService(uow).get(user.id)).balance == Decimal("10.00")
    async with uow() as repos:
        entries = await repos["billing"].list_ledger_entries(user.id)
    assert [entry.kind for entry in reversed(entries)] == [
        LedgerKind.OPENING_BALANCE.value,
        LedgerKind.CONFIG_RESERVATION.value,
    ]


@pytest.mark.asyncio
async def test_rejected_non_provision_operation_never_refunds_creation(
    monkeypatch, sessionmaker
):
    user, server = await _user_and_server(balance=Decimal("20.00"))
    billing = BillingService(uow, per_config_cost="1.00")
    gateway = BillingGateway()
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *args, **kwargs: gateway
    )
    cfg = await billing.create_paid_config(
        server_id=server.id,
        owner_id=user.id,
        name="paid-before-suspend",
        display_name="Paid",
        creation_cost="10.00",
    )

    gateway.behavior = "rejected"
    with pytest.raises(APIRequestRejectedError):
        await billing._config_service.suspend_config(cfg.id)

    _, refunded = await billing.reconcile_pending_config_operations()
    assert refunded == 0
    assert (await UserService(uow).get(user.id)).balance == Decimal("10.00")


@pytest.mark.asyncio
async def test_insufficient_reservation_rolls_back_staged_provision(sessionmaker):
    user, server = await _user_and_server(balance=Decimal("5.00"))
    billing = BillingService(uow, per_config_cost="1.00")

    with pytest.raises(InsufficientBalanceError):
        await billing.create_paid_config(
            server_id=server.id,
            owner_id=user.id,
            name="must-rollback",
            display_name="Must rollback",
            creation_cost="10.00",
        )

    async with uow() as repos:
        assert await repos["configs"].get(name="must-rollback") is None
        assert await repos["vpn_operations"].list() == []
        entries = await repos["billing"].list_ledger_entries(user.id)
    assert [entry.kind for entry in entries] == [LedgerKind.OPENING_BALANCE.value]


@pytest.mark.asyncio
async def test_financial_kill_switches_are_enforced(monkeypatch, sessionmaker):
    user, server = await _user_and_server(balance=Decimal("10.00"))
    billing = BillingService(uow, per_config_cost="1.00")
    monkeypatch.setattr(settings, "maintenance_mode", True)

    assert await billing.charge_all() == {}
    with pytest.raises(InvalidOperationError):
        await billing.create_paid_config(
            server_id=server.id,
            owner_id=user.id,
            name="maintenance",
            display_name="Maintenance",
            creation_cost="1.00",
        )


@pytest.mark.asyncio
async def test_lifecycle_failure_does_not_rollback_committed_money(sessionmaker):
    user = await UserService(uow).register(104)
    billing = BillingService(uow, per_config_cost="1.00")

    async def broken_lifecycle(user_id):
        raise APITransportError("manager unavailable")

    billing._config_service.unsuspend_all = broken_lifecycle
    updated = await billing.top_up(
        user.id, "5.00", idempotency_key="lifecycle-failure:104"
    )
    assert updated.balance == Decimal("5.00")
    assert (await UserService(uow).get(user.id)).balance == Decimal("5.00")
