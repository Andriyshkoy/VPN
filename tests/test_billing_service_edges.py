from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.db.unit_of_work import uow
from core.exceptions import (
    InsufficientBalanceError,
    ServerNotFoundError,
    UserNotFoundError,
)
from core.services.billing import BillingService
from core.services.server import ServerService
from core.services.user import UserService


class DummyGateway:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    async def create_client(self, name, use_password=False):
        pass

    async def download_config(self, name):
        return b"data"

    async def revoke_client(self, name):
        pass

    async def suspend_client(self, name):
        pass

    async def unsuspend_client(self, name):
        pass

    async def list_blocked(self):
        return []


@pytest.mark.asyncio
async def test_billing_settings_defaults_and_update(sessionmaker):
    billing = BillingService(uow)

    settings = await billing.get_settings()
    assert settings.config_creation_cost == Decimal("10")
    assert settings.monthly_config_cost == Decimal("50")
    assert settings.referral_first_deposit_bonus_pct == Decimal("50")
    assert settings.referral_recurring_bonus_pct == Decimal("10")

    updated = await billing.update_settings(config_creation_cost=15)
    assert updated.config_creation_cost == Decimal("15")
    assert updated.monthly_config_cost == Decimal("50")

    updated = await billing.update_settings(
        referral_first_deposit_bonus_pct=25, referral_recurring_bonus_pct=5
    )
    assert updated.referral_first_deposit_bonus_pct == Decimal("25")
    assert updated.referral_recurring_bonus_pct == Decimal("5")


@pytest.mark.asyncio
async def test_topup_rounds_and_creates_transaction(sessionmaker):
    user_svc = UserService(uow)
    billing = BillingService(uow)

    user = await user_svc.register(1, username="user")
    await billing.top_up(user.id, 10.005, source="test", description="round")

    updated = await user_svc.get(user.id)
    assert updated.balance == Decimal("10.01")

    txs = await billing.list_transactions(user_id=user.id)
    assert txs and txs[0].amount == Decimal("10.01")
    assert txs[0].kind == "topup"
    assert txs[0].source == "test"


@pytest.mark.asyncio
async def test_topup_invalid_amount_raises(sessionmaker):
    user_svc = UserService(uow)
    billing = BillingService(uow)

    user = await user_svc.register(2)
    with pytest.raises(ValueError):
        await billing.top_up(user.id, 0)
    with pytest.raises(ValueError):
        await billing.top_up(user.id, 0.004)


@pytest.mark.asyncio
async def test_withdraw_rounds_and_updates_balance(sessionmaker):
    user_svc = UserService(uow)
    billing = BillingService(uow)

    user = await user_svc.register(3)
    await billing.top_up(user.id, 10.01)
    await billing.withdraw(user.id, 5.005, source="test")

    updated = await user_svc.get(user.id)
    assert updated.balance == Decimal("5.00")

    txs = await billing.list_transactions(user_id=user.id)
    withdraw_txs = [tx for tx in txs if tx.kind == "withdraw"]
    assert withdraw_txs and withdraw_txs[0].amount == Decimal("-5.01")


@pytest.mark.asyncio
async def test_withdraw_insufficient_balance(sessionmaker):
    user_svc = UserService(uow)
    billing = BillingService(uow)

    user = await user_svc.register(4)
    await billing.top_up(user.id, 1)
    with pytest.raises(InsufficientBalanceError):
        await billing.withdraw(user.id, 2)

    txs = await billing.list_transactions(user_id=user.id)
    withdraw_txs = [tx for tx in txs if tx.kind == "withdraw"]
    assert not withdraw_txs


@pytest.mark.asyncio
async def test_withdraw_user_not_found(sessionmaker):
    billing = BillingService(uow)

    with pytest.raises(UserNotFoundError):
        await billing.withdraw(999, 1)


@pytest.mark.asyncio
async def test_create_paid_config_rolls_back_on_api_error(
    monkeypatch, sessionmaker
):
    class FailingGateway(DummyGateway):
        async def create_client(self, name, use_password=False):
            raise RuntimeError("boom")

    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *a, **kw: FailingGateway()
    )

    server_svc = ServerService(uow)
    user_svc = UserService(uow)
    billing = BillingService(uow)
    await billing.update_settings(config_creation_cost=10)

    user = await user_svc.register(10)
    server = await server_svc.create(
        name="srv",
        ip="1.1.1.1",
        port=22,
        host="host",
        location="US",
        api_key="k",
        cost=1,
    )

    await billing.top_up(user.id, 10)

    with pytest.raises(RuntimeError):
        await billing.create_paid_config(
            server_id=server.id,
            owner_id=user.id,
            name="cfg",
            display_name="disp",
        )

    updated = await user_svc.get(user.id)
    assert updated.balance == Decimal("10")

    async with uow() as repos:
        cfgs = await repos["configs"].list(owner_id=user.id)
    assert cfgs == []

    txs = await billing.list_transactions(user_id=user.id)
    assert [tx for tx in txs if tx.kind == "config_creation"] == []


@pytest.mark.asyncio
async def test_create_paid_config_missing_entities(monkeypatch, sessionmaker):
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *a, **kw: DummyGateway()
    )

    server_svc = ServerService(uow)
    user_svc = UserService(uow)
    billing = BillingService(uow)

    user = await user_svc.register(11)
    server = await server_svc.create(
        name="srv2",
        ip="1.1.1.1",
        port=22,
        host="host",
        location="US",
        api_key="k",
        cost=1,
    )

    await billing.top_up(user.id, 20)

    with pytest.raises(ServerNotFoundError):
        await billing.create_paid_config(
            server_id=999,
            owner_id=user.id,
            name="cfg",
            display_name="disp",
        )

    with pytest.raises(UserNotFoundError):
        await billing.create_paid_config(
            server_id=server.id,
            owner_id=999,
            name="cfg2",
            display_name="disp2",
        )


@pytest.mark.asyncio
async def test_charge_usage_no_charge_when_monthly_zero(sessionmaker):
    user_svc = UserService(uow)
    server_svc = ServerService(uow)
    billing = BillingService(uow)

    user = await user_svc.register(20)
    server = await server_svc.create(
        name="srv3",
        ip="1.1.1.1",
        port=22,
        host="host",
        location="US",
        api_key="k",
        cost=1,
    )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with uow() as repos:
        cfg = await repos["configs"].create(server.id, user.id, "cfg", "disp")
        cfg.last_billed_at = now - timedelta(hours=3)

    await billing.update_settings(monthly_config_cost=0)
    charges = await billing.charge_usage(now=now)

    assert charges == {}
    async with uow() as repos:
        cfg_db = await repos["configs"].get(id=cfg.id)
    assert cfg_db.last_billed_at == cfg.last_billed_at

    txs = await billing.list_transactions(user_id=user.id)
    assert txs == []


@pytest.mark.asyncio
async def test_charge_usage_rounding_and_ledger(sessionmaker):
    user_svc = UserService(uow)
    server_svc = ServerService(uow)
    billing = BillingService(uow)
    await billing.update_settings(monthly_config_cost=100)

    user = await user_svc.register(21)
    server = await server_svc.create(
        name="srv4",
        ip="1.1.1.1",
        port=22,
        host="host",
        location="US",
        api_key="k",
        cost=1,
    )

    await billing.top_up(user.id, 1)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with uow() as repos:
        cfg = await repos["configs"].create(server.id, user.id, "cfg2", "disp2")
        cfg.last_billed_at = now - timedelta(hours=2)

    charges = await billing.charge_usage(now=now)
    assert list(charges.values()) == [Decimal("0.28")]

    updated = await user_svc.get(user.id)
    assert updated.balance == Decimal("0.72")

    txs = await billing.list_transactions(user_id=user.id)
    usage = [tx for tx in txs if tx.kind == "usage"]
    assert usage and usage[0].amount == Decimal("-0.28")

    async with uow() as repos:
        cfg_db = await repos["configs"].get(id=cfg.id)
    assert cfg_db.last_billed_at == now


@pytest.mark.asyncio
async def test_charge_usage_skips_under_one_hour(sessionmaker):
    user_svc = UserService(uow)
    server_svc = ServerService(uow)
    billing = BillingService(uow)

    user = await user_svc.register(22)
    server = await server_svc.create(
        name="srv5",
        ip="1.1.1.1",
        port=22,
        host="host",
        location="US",
        api_key="k",
        cost=1,
    )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with uow() as repos:
        cfg = await repos["configs"].create(server.id, user.id, "cfg3", "disp3")
        cfg.last_billed_at = now - timedelta(minutes=30)

    charges = await billing.charge_usage(now=now)
    assert charges == {}

    txs = await billing.list_transactions(user_id=user.id)
    assert txs == []


@pytest.mark.asyncio
async def test_create_paid_config_creates_transaction(monkeypatch, sessionmaker):
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *a, **kw: DummyGateway()
    )

    server_svc = ServerService(uow)
    user_svc = UserService(uow)
    billing = BillingService(uow)
    await billing.update_settings(config_creation_cost=7)

    user = await user_svc.register(30)
    server = await server_svc.create(
        name="srv6",
        ip="1.1.1.1",
        port=22,
        host="host",
        location="US",
        api_key="k",
        cost=1,
    )

    await billing.top_up(user.id, 20)
    cfg = await billing.create_paid_config(
        server_id=server.id,
        owner_id=user.id,
        name="cfg6",
        display_name="disp6",
    )

    txs = await billing.list_transactions(user_id=user.id)
    creation = [tx for tx in txs if tx.kind == "config_creation"]
    assert creation and creation[0].config_id == cfg.id
    assert creation[0].amount == Decimal("-7.00")

    updated = await user_svc.get(user.id)
    assert updated.balance == Decimal("13.00")


@pytest.mark.asyncio
async def test_withdraw_suspends_active_configs(monkeypatch, sessionmaker):
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *a, **kw: DummyGateway()
    )

    server_svc = ServerService(uow)
    user_svc = UserService(uow)
    billing = BillingService(uow)

    user = await user_svc.register(31)
    server = await server_svc.create(
        name="srv7",
        ip="1.1.1.1",
        port=22,
        host="host",
        location="US",
        api_key="k",
        cost=1,
    )

    async with uow() as repos:
        cfg = await repos["configs"].create(server.id, user.id, "cfg7", "disp7")

    await billing.top_up(user.id, 5)
    await billing.withdraw(user.id, 5)

    async with uow() as repos:
        cfg_db = await repos["configs"].get(id=cfg.id)
    assert cfg_db.suspended is True


@pytest.mark.asyncio
async def test_charge_usage_multiple_configs(sessionmaker):
    user_svc = UserService(uow)
    server_svc = ServerService(uow)
    billing = BillingService(uow)
    await billing.update_settings(monthly_config_cost=720)

    user = await user_svc.register(32)
    server = await server_svc.create(
        name="srv8",
        ip="1.1.1.1",
        port=22,
        host="host",
        location="US",
        api_key="k",
        cost=1,
    )

    await billing.top_up(user.id, 10)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with uow() as repos:
        cfg1 = await repos["configs"].create(server.id, user.id, "cfg8a", "disp8a")
        cfg2 = await repos["configs"].create(server.id, user.id, "cfg8b", "disp8b")
        cfg1.last_billed_at = now - timedelta(hours=1)
        cfg2.last_billed_at = now - timedelta(hours=1)

    charges = await billing.charge_usage(now=now)
    assert list(charges.values()) == [Decimal("2.00")]

    txs = await billing.list_transactions(user_id=user.id)
    usage = [tx for tx in txs if tx.kind == "usage"]
    assert len(usage) == 2
    assert sum(tx.amount for tx in usage) == Decimal("-2.00")

    updated = await user_svc.get(user.id)
    assert updated.balance == Decimal("8.00")
