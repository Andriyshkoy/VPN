from datetime import datetime, timedelta, timezone

import pytest

from core.db.unit_of_work import uow
from core.exceptions import ConfigNotFoundError, ServerNotFoundError, UserNotFoundError
from core.services.config import ConfigService
from core.services.server import ServerService
from core.services.user import UserService


class DummyGateway:
    created = []
    suspended = []
    unsuspended = []
    revoked = []
    downloaded = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    async def create_client(self, name, use_password=False):
        DummyGateway.created.append((name, use_password))

    async def download_config(self, name):
        DummyGateway.downloaded.append(name)
        return b"data"

    async def revoke_client(self, name):
        DummyGateway.revoked.append(name)

    async def suspend_client(self, name):
        DummyGateway.suspended.append(name)

    async def unsuspend_client(self, name):
        DummyGateway.unsuspended.append(name)

    async def list_blocked(self):
        return ["blocked"]


@pytest.mark.asyncio
async def test_create_config_calls_gateway(monkeypatch, sessionmaker):
    DummyGateway.created = []
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *a, **kw: DummyGateway()
    )

    user_svc = UserService(uow)
    server_svc = ServerService(uow)
    config_svc = ConfigService(uow)

    user = await user_svc.register(1)
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
        use_password=True,
    )

    assert cfg.name == "cfg"
    assert DummyGateway.created == [("cfg", True)]


@pytest.mark.asyncio
async def test_create_config_missing_entities(monkeypatch, sessionmaker):
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *a, **kw: DummyGateway()
    )

    user_svc = UserService(uow)
    server_svc = ServerService(uow)
    config_svc = ConfigService(uow)

    user = await user_svc.register(2)
    server = await server_svc.create(
        name="srv2",
        ip="1.1.1.1",
        port=22,
        host="host",
        location="US",
        api_key="k",
        cost=1,
    )

    with pytest.raises(ServerNotFoundError):
        await config_svc.create_config(
            server_id=999,
            owner_id=user.id,
            name="cfg",
            display_name="disp",
        )

    with pytest.raises(UserNotFoundError):
        await config_svc.create_config(
            server_id=server.id,
            owner_id=999,
            name="cfg",
            display_name="disp",
        )


@pytest.mark.asyncio
async def test_config_lifecycle(monkeypatch, sessionmaker):
    DummyGateway.suspended = []
    DummyGateway.unsuspended = []
    DummyGateway.revoked = []
    DummyGateway.downloaded = []
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *a, **kw: DummyGateway()
    )

    user_svc = UserService(uow)
    server_svc = ServerService(uow)
    config_svc = ConfigService(uow)

    user = await user_svc.register(3)
    server = await server_svc.create(
        name="srv3",
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
        name="cfg2",
        display_name="disp2",
    )

    await config_svc.suspend_config(cfg.id)
    suspended = await config_svc.get(cfg.id)
    assert suspended and suspended.suspended

    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    async with uow() as repos:
        cfg_db = await repos["configs"].get(id=cfg.id)
        cfg_db.last_billed_at = old

    await config_svc.unsuspend_config(cfg.id)
    unsuspended = await config_svc.get(cfg.id)
    assert unsuspended and not unsuspended.suspended

    async with uow() as repos:
        cfg_db = await repos["configs"].get(id=cfg.id)
    assert cfg_db.last_billed_at > old

    renamed = await config_svc.rename_config(cfg.id, "newname")
    assert renamed.display_name == "newname"


@pytest.mark.asyncio
async def test_download_and_revoke_config(monkeypatch, sessionmaker):
    DummyGateway.downloaded = []
    DummyGateway.revoked = []
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *a, **kw: DummyGateway()
    )

    user_svc = UserService(uow)
    server_svc = ServerService(uow)
    config_svc = ConfigService(uow)

    user = await user_svc.register(4)
    server = await server_svc.create(
        name="srv4",
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
        name="cfg4",
        display_name="disp4",
    )

    data = await config_svc.download_config(cfg.id)
    assert data == b"data"
    assert DummyGateway.downloaded == ["cfg4"]

    await config_svc.revoke_config(cfg.id)
    assert DummyGateway.revoked == ["cfg4"]

    async with uow() as repos:
        assert await repos["configs"].get(id=cfg.id) is None


@pytest.mark.asyncio
async def test_suspend_all_and_unsuspend_all(monkeypatch, sessionmaker):
    DummyGateway.suspended = []
    DummyGateway.unsuspended = []
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *a, **kw: DummyGateway()
    )

    user_svc = UserService(uow)
    server_svc = ServerService(uow)
    config_svc = ConfigService(uow)

    user = await user_svc.register(5)
    server = await server_svc.create(
        name="srv5",
        ip="1.1.1.1",
        port=22,
        host="host",
        location="US",
        api_key="k",
        cost=1,
    )

    cfg1 = await config_svc.create_config(
        server_id=server.id,
        owner_id=user.id,
        name="cfg5a",
        display_name="disp5a",
    )
    cfg2 = await config_svc.create_config(
        server_id=server.id,
        owner_id=user.id,
        name="cfg5b",
        display_name="disp5b",
    )

    suspended_count = await config_svc.suspend_all(user.id)
    assert suspended_count == 2
    assert set(DummyGateway.suspended) == {"cfg5a", "cfg5b"}

    unsuspended_count = await config_svc.unsuspend_all(user.id)
    assert unsuspended_count == 2
    assert set(DummyGateway.unsuspended) == {"cfg5a", "cfg5b"}


@pytest.mark.asyncio
async def test_config_missing_errors(monkeypatch, sessionmaker):
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *a, **kw: DummyGateway()
    )

    config_svc = ConfigService(uow)

    with pytest.raises(ConfigNotFoundError):
        await config_svc.download_config(999)
    with pytest.raises(ConfigNotFoundError):
        await config_svc.suspend_config(999)
    with pytest.raises(ConfigNotFoundError):
        await config_svc.unsuspend_config(999)
    with pytest.raises(ConfigNotFoundError):
        await config_svc.rename_config(999, "name")


@pytest.mark.asyncio
async def test_list_blocked_missing_server(monkeypatch, sessionmaker):
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *a, **kw: DummyGateway()
    )

    config_svc = ConfigService(uow)

    with pytest.raises(ServerNotFoundError):
        await config_svc.list_blocked(999)


@pytest.mark.asyncio
async def test_list_blocked_returns_data(monkeypatch, sessionmaker):
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *a, **kw: DummyGateway()
    )

    server_svc = ServerService(uow)
    config_svc = ConfigService(uow)

    server = await server_svc.create(
        name="srv6",
        ip="1.1.1.1",
        port=22,
        host="host",
        location="US",
        api_key="k",
        cost=1,
    )

    blocked = await config_svc.list_blocked(server.id)
    assert blocked == ["blocked"]
