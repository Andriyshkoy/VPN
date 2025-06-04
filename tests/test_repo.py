import pytest
from core.db.models import User, Server
from core.db.repo import UserRepo, ServerRepo, ConfigRepo
from core.db.unit_of_work import uow

@pytest.mark.asyncio
async def test_user_repo_get_or_create(session):
    repo = UserRepo(session)
    user = await repo.get_or_create(123, username="alice")
    assert user.id is not None
    assert user.username == "alice"

    user_again = await repo.get_or_create(123)
    assert user_again.id == user.id


@pytest.mark.asyncio
async def test_user_repo_search(session):
    repo = UserRepo(session)
    await repo.get_or_create(1, username="alice")
    await repo.get_or_create(2, username="alex")
    await repo.get_or_create(3, username="bob")

    results = await repo.search_by_username("al")
    usernames = {u.username for u in results}
    assert usernames == {"alice", "alex"}


@pytest.mark.asyncio
async def test_server_repo_crud(session):
    repo = ServerRepo(session)
    server = await repo.create(
        name="vpn1",
        ip="1.1.1.1",
        port=22,
        host="host1",
        location="USA",
        api_key="secret",
        cost=10,
    )
    assert server.id is not None
    # api_key is stored encrypted but returned decrypted
    assert server.api_key == "secret"

    updated = await repo.update(server.id, name="vpn2", location="Canada")
    assert updated.name == "vpn2"
    assert updated.location == "Canada"

    by_name = await repo.search_by_name("vpn2")
    assert by_name[0].id == server.id

    by_loc = await repo.search_by_location("can")
    assert by_loc[0].id == server.id


@pytest.mark.asyncio
async def test_config_repo(session):
    user_repo = UserRepo(session)
    server_repo = ServerRepo(session)
    config_repo = ConfigRepo(session)

    user = await user_repo.get_or_create(1, username="user")
    server = await server_repo.create(
        name="vpn",
        ip="1.1.1.1",
        port=22,
        host="host",
        location="US",
        api_key="k",
        cost=1,
    )
    cfg = await config_repo.create(server.id, user.id, "cfg1", "display")

    active = await config_repo.get_active()
    assert len(active) == 1 and active[0].id == cfg.id

    await config_repo.suspend(cfg.id)
    assert cfg.suspended is True

    suspended = await config_repo.get_suspended()
    assert len(suspended) == 1

    await config_repo.unsuspend(cfg.id)
    assert cfg.suspended is False


@pytest.mark.asyncio
async def test_uow(monkeypatch, engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    import core.db as db

    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "async_session", maker, raising=False)
    monkeypatch.setattr(db.unit_of_work, "async_session", maker, raising=False)

    async with uow() as repos:
        assert set(repos.keys()) == {"users", "servers", "configs"}
        await repos["users"].add(User(tg_id=1))

    async with maker() as sess:
        repo = UserRepo(sess)
        user = await repo.get(tg_id=1)
        assert user is not None
