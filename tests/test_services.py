import pytest
from decimal import Decimal

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
    billing = BillingService(uow, per_config_cost=1)

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
            creation_cost=10,
        )


@pytest.mark.asyncio
async def test_services_workflow(monkeypatch, sessionmaker):
    monkeypatch.setattr("core.services.config.APIGateway", lambda *a, **kw: DummyGateway())

    server_svc = ServerService(uow)
    user_svc = UserService(uow)
    config_svc = ConfigService(uow)
    billing = BillingService(uow, per_config_cost=1)

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
        creation_cost=10,
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
    billing = BillingService(uow, per_config_cost=3)

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
        creation_cost=10,
    )
    await billing.create_paid_config(
        server_id=server.id,
        owner_id=user.id,
        name="c2",
        display_name="d2",
        creation_cost=10,
    )

    await billing.charge_all()

    updated = await user_svc.get(user.id)
    assert updated.balance == 4


@pytest.mark.asyncio
async def test_charge_all_returns_dict(monkeypatch, sessionmaker):
    monkeypatch.setattr("core.services.config.APIGateway", lambda *a, **kw: DummyGateway())

    server_svc = ServerService(uow)
    user_svc = UserService(uow)
    billing = BillingService(uow, per_config_cost=2)

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
    await billing.create_paid_config(
        server_id=server.id,
        owner_id=user.id,
        name="cfg",
        display_name="disp",
        creation_cost=1,
    )

    charges = await billing.charge_all()
    assert list(charges.values()) == [Decimal(2)]
    charged_user = next(iter(charges))
    assert charged_user.id == user.id


@pytest.mark.asyncio
async def test_billing_suspend_unsuspend(monkeypatch, sessionmaker):
    monkeypatch.setattr("core.services.config.APIGateway", lambda *a, **kw: DummyGateway())

    server_svc = ServerService(uow)
    user_svc = UserService(uow)
    config_svc = ConfigService(uow)
    billing = BillingService(uow, per_config_cost=3)

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
        creation_cost=10,
    )

    await billing.charge_all()

    suspended = await config_svc.list_suspended(owner_id=user.id)
    assert len(suspended) == 1 and suspended[0].id == cfg.id

    await billing.top_up(user.id, 5)

    active = await config_svc.list_active(owner_id=user.id)
    assert len(active) == 1 and active[0].id == cfg.id


@pytest.mark.asyncio
async def test_server_update_and_user_with_configs(monkeypatch, sessionmaker):
    monkeypatch.setattr("core.services.config.APIGateway", lambda *a, **kw: DummyGateway())

    server_svc = ServerService(uow)
    user_svc = UserService(uow)
    config_svc = ConfigService(uow)
    billing = BillingService(uow, per_config_cost=1)

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
        creation_cost=10,
    )

    user_data, configs = await user_svc.get_with_configs(user.id)

    assert user_data.id == user.id
    assert configs and configs[0].id == cfg.id
