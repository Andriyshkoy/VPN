import pytest

from core.db.unit_of_work import uow
from core.exceptions import UserNotFoundError
from core.services.config import ConfigService
from core.services.server import ServerService
from core.services.user import UserService


@pytest.mark.asyncio
async def test_register_updates_username(sessionmaker):
    user_svc = UserService(uow)

    user = await user_svc.register(1, username="old")
    assert user.username == "old"

    updated = await user_svc.register(1, username="new")
    assert updated.username == "new"


@pytest.mark.asyncio
async def test_register_referral_link(sessionmaker):
    user_svc = UserService(uow)

    referrer = await user_svc.register(10, username="ref")
    referred = await user_svc.register(11, username="child", ref_id=10)

    assert referred.id != referrer.id

    async with uow() as repos:
        referred_db = await repos["users"].get(id=referred.id)
    assert referred_db.referred_by_id == referrer.id


@pytest.mark.asyncio
async def test_register_invalid_referral(sessionmaker):
    user_svc = UserService(uow)

    referred = await user_svc.register(12, username="child", ref_id=999)
    async with uow() as repos:
        referred_db = await repos["users"].get(id=referred.id)
    assert referred_db.referred_by_id is None


@pytest.mark.asyncio
async def test_get_referrer_and_referrals(sessionmaker):
    user_svc = UserService(uow)

    referrer = await user_svc.register(13, username="ref")
    referred = await user_svc.register(14, username="child", ref_id=13)

    referrals = await user_svc.get_referrals(referrer.id)
    assert len(referrals) == 1
    assert referrals[0].id == referred.id

    count = await user_svc.count_referrals(referrer.id)
    assert count == 1

    fetched_referrer = await user_svc.get_refferer(referred.id)
    assert fetched_referrer and fetched_referrer.id == referrer.id


@pytest.mark.asyncio
async def test_delete_missing_user(sessionmaker):
    user_svc = UserService(uow)

    with pytest.raises(UserNotFoundError):
        await user_svc.delete(999)


@pytest.mark.asyncio
async def test_delete_suspends_configs(sessionmaker, monkeypatch):
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

    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *a, **kw: DummyGateway()
    )

    user_svc = UserService(uow)
    server_svc = ServerService(uow)
    config_svc = ConfigService(uow)

    user = await user_svc.register(20)
    server = await server_svc.create(
        name="srv",
        ip="1.1.1.1",
        port=22,
        host="host",
        location="US",
        api_key="k",
        cost=1,
    )

    cfg = await config_svc.create_config(
        server_id=server.id,
        owner_id=user.id,
        name="cfg",
        display_name="disp",
    )

    await user_svc.delete(user.id)

    async with uow() as repos:
        cfg_db = await repos["configs"].get(id=cfg.id)
        user_db = await repos["users"].get(id=user.id)

    assert cfg_db.suspended is True
    assert user_db is None
