from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.db.unit_of_work import uow
from core.exceptions import InsufficientBalanceError
from core.services import BillingService, ConfigService, ServerService, UserService


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
async def test_create_requires_balance(monkeypatch, sessionmaker):
    monkeypatch.setattr("core.services.config.APIGateway", lambda *a, **kw: DummyGateway())

    server_svc = ServerService(uow)
    user_svc = UserService(uow)
    config_svc = ConfigService(uow)
    billing = BillingService(uow)

    user = await user_svc.register(55)
    server = await server_svc.create(
        name="srvbalance",
        ip="1.1.1.1",
        port=22,
        host="host",
        location="US",
        api_key="k",
        cost=1,
    )

    with pytest.raises(InsufficientBalanceError):
        await billing.create_paid_config(
            server_id=server.id,
            owner_id=user.id,
            name="bal",
            display_name="disp",
        )


@pytest.mark.asyncio
async def test_services_workflow(monkeypatch, sessionmaker):
    monkeypatch.setattr("core.services.config.APIGateway", lambda *a, **kw: DummyGateway())

    server_svc = ServerService(uow)
    user_svc = UserService(uow)
    config_svc = ConfigService(uow)
    billing = BillingService(uow)

    # create user and server
    user = await user_svc.register(100)
    server = await server_svc.create(
        name="srv",
        ip="1.1.1.1",
        port=22,
        host="host",
        location="US",
        api_key="k",
        cost=1,
    )

    await billing.top_up(user.id, 20)

    # create config
    cfg = await billing.create_paid_config(
        server_id=server.id,
        owner_id=user.id,
        name="cfg1",
        display_name="disp",
    )

    # suspend/unsuspend
    await config_svc.suspend_config(cfg.id)
    sus = await config_svc.list_suspended(owner_id=user.id)
    assert len(sus) == 1 and sus[0].id == cfg.id

    await config_svc.unsuspend_config(cfg.id)
    active = await config_svc.list_active(owner_id=user.id)
    assert len(active) == 1 and active[0].id == cfg.id

    # delete user -> config suspended but kept
    await user_svc.delete(user.id)
    sus2 = await config_svc.list_suspended(owner_id=user.id)
    assert len(sus2) == 1 and sus2[0].id == cfg.id

    # delete server -> config removed
    await server_svc.delete(server.id)
    async with uow() as repos:
        assert await repos["configs"].get(id=cfg.id) is None


@pytest.mark.asyncio
async def test_billing(monkeypatch, sessionmaker):
    monkeypatch.setattr("core.services.config.APIGateway", lambda *a, **kw: DummyGateway())

    server_svc = ServerService(uow)
    user_svc = UserService(uow)
    config_svc = ConfigService(uow)
    billing = BillingService(uow)
    await billing.update_settings(config_creation_cost=10, monthly_config_cost=2160)

    user = await user_svc.register(10)
    server = await server_svc.create(
        name="srv2",
        ip="1.1.1.1",
        port=22,
        host="host",
        location="US",
        api_key="k",
        cost=1,
    )

    await billing.top_up(user.id, 30)

    await billing.create_paid_config(
        server_id=server.id,
        owner_id=user.id,
        name="c1",
        display_name="d1",
    )
    await billing.create_paid_config(
        server_id=server.id,
        owner_id=user.id,
        name="c2",
        display_name="d2",
    )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with uow() as repos:
        cfgs = await repos["configs"].list(owner_id=user.id)
        for cfg in cfgs:
            cfg.last_billed_at = now - timedelta(hours=1)
    await billing.charge_usage(now=now)

    updated = await user_svc.get(user.id)
    assert updated.balance == 4


@pytest.mark.asyncio
async def test_charge_all_returns_dict(monkeypatch, sessionmaker):
    monkeypatch.setattr("core.services.config.APIGateway", lambda *a, **kw: DummyGateway())

    server_svc = ServerService(uow)
    user_svc = UserService(uow)
    billing = BillingService(uow)
    await billing.update_settings(config_creation_cost=1, monthly_config_cost=720)

    user = await user_svc.register(99)
    server = await server_svc.create(
        name="srvret",
        ip="1.1.1.1",
        port=22,
        host="host",
        location="US",
        api_key="k",
        cost=1,
    )

    await billing.top_up(user.id, 10)
    cfg = await billing.create_paid_config(
        server_id=server.id,
        owner_id=user.id,
        name="cfg",
        display_name="disp",
    )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with uow() as repos:
        cfg_db = await repos["configs"].get(id=cfg.id)
        cfg_db.last_billed_at = now - timedelta(hours=1)
    charges = await billing.charge_usage(now=now)
    assert list(charges.values()) == [Decimal("1.00")]
    charged_user = next(iter(charges))
    assert charged_user.id == user.id


@pytest.mark.asyncio
async def test_billing_suspend_unsuspend(monkeypatch, sessionmaker):
    monkeypatch.setattr("core.services.config.APIGateway", lambda *a, **kw: DummyGateway())

    server_svc = ServerService(uow)
    user_svc = UserService(uow)
    config_svc = ConfigService(uow)
    billing = BillingService(uow)
    await billing.update_settings(config_creation_cost=10, monthly_config_cost=2160)

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

    await billing.top_up(user.id, 13)

    cfg = await billing.create_paid_config(
        server_id=server.id,
        owner_id=user.id,
        name="c3",
        display_name="d3",
    )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with uow() as repos:
        cfg_db = await repos["configs"].get(id=cfg.id)
        cfg_db.last_billed_at = now - timedelta(hours=1)
    await billing.charge_usage(now=now)

    suspended = await config_svc.list_suspended(owner_id=user.id)
    assert len(suspended) == 1 and suspended[0].id == cfg.id

    await billing.top_up(user.id, 5)

    active = await config_svc.list_active(owner_id=user.id)
    assert len(active) == 1 and active[0].id == cfg.id


@pytest.mark.asyncio
async def test_referral_bonus_on_topup(sessionmaker):
    user_svc = UserService(uow)
    billing = BillingService(uow)
    await billing.update_settings(
        referral_first_deposit_bonus_pct=50, referral_recurring_bonus_pct=10
    )

    referrer = await user_svc.register(1000, username="referrer")
    referred = await user_svc.register(2000, username="referred", ref_id=1000)

    await billing.top_up(referred.id, 100, source="telegram_pay")
    await billing.top_up(referred.id, 100, source="telegram_pay")

    updated_referrer = await user_svc.get(referrer.id)
    assert updated_referrer.balance == Decimal("60.00")

    txs = await billing.list_transactions(user_id=referrer.id)
    bonus_txs = [tx for tx in txs if tx.kind == "referral_bonus"]
    assert len(bonus_txs) == 2
    assert {tx.amount for tx in bonus_txs} == {
        Decimal("50.00"),
        Decimal("10.00"),
    }
    assert all(tx.related_user_id == referred.id for tx in bonus_txs)


@pytest.mark.asyncio
async def test_server_update_and_user_with_configs(monkeypatch, sessionmaker):
    monkeypatch.setattr("core.services.config.APIGateway", lambda *a, **kw: DummyGateway())

    server_svc = ServerService(uow)
    user_svc = UserService(uow)
    config_svc = ConfigService(uow)
    billing = BillingService(uow)

    user = await user_svc.register(200)
    server = await server_svc.create(
        name="srv4", ip="1.1.1.1", port=22, host="host", location="US", api_key="k", cost=1
    )

    await billing.top_up(user.id, 20)

    await server_svc.update(server.id, name="newname")

    cfg = await billing.create_paid_config(
        server_id=server.id,
        owner_id=user.id,
        name="cfg4",
        display_name="d4",
    )

    user_data, configs = await user_svc.get_with_configs(user.id)

    assert user_data.id == user.id
    assert configs and configs[0].id == cfg.id
