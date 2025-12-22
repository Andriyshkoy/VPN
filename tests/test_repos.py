from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.db.unit_of_work import uow


@pytest.mark.asyncio
async def test_billing_settings_repo_get_or_create_and_update(sessionmaker):
    async with uow() as repos:
        settings = await repos["billing_settings"].get_or_create()
        assert settings.id == 1
        assert settings.config_creation_cost == Decimal("10")

        updated = await repos["billing_settings"].update(
            config_creation_cost=Decimal("12"),
            monthly_config_cost=Decimal("34"),
        )
        assert updated.config_creation_cost == Decimal("12")
        assert updated.monthly_config_cost == Decimal("34")


@pytest.mark.asyncio
async def test_user_repo_search_by_username(sessionmaker):
    async with uow() as repos:
        await repos["users"].get_or_create(1, username="Alice")
        await repos["users"].get_or_create(2, username="bob")

        results = await repos["users"].search_by_username("ali")
        assert len(results) == 1
        assert results[0].username == "Alice"


@pytest.mark.asyncio
async def test_server_repo_search_by_name_and_location(sessionmaker):
    async with uow() as repos:
        await repos["servers"].create(
            name="EU-West",
            ip="1.1.1.1",
            port=22,
            host="host1",
            location="Europe",
            api_key="k1",
            cost=1,
        )
        await repos["servers"].create(
            name="US-East",
            ip="2.2.2.2",
            port=22,
            host="host2",
            location="United States",
            api_key="k2",
            cost=1,
        )

        results = await repos["servers"].search_by_name("west")
        assert len(results) == 1
        assert results[0].name == "EU-West"

        results = await repos["servers"].search_by_location("states")
        assert len(results) == 1
        assert results[0].name == "US-East"


@pytest.mark.asyncio
async def test_config_repo_list_billable_and_advance(sessionmaker):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with uow() as repos:
        server = await repos["servers"].create(
            name="srv",
            ip="1.1.1.1",
            port=22,
            host="host",
            location="US",
            api_key="k",
            cost=1,
        )
        user = await repos["users"].get_or_create(3)
        cfg1 = await repos["configs"].create(server.id, user.id, "cfg1", "disp1")
        cfg2 = await repos["configs"].create(server.id, user.id, "cfg2", "disp2")
        cfg1.last_billed_at = now - timedelta(hours=2)
        cfg2.last_billed_at = now - timedelta(hours=2)
        cfg2.suspended = True

    async with uow() as repos:
        billable = await repos["configs"].list_billable(before=now - timedelta(hours=1))
        assert [cfg.id for cfg in billable] == [cfg1.id]

        ok = await repos["configs"].advance_billing(
            cfg1.id, cfg1.last_billed_at, now
        )
        assert ok is True
        failed = await repos["configs"].advance_billing(
            cfg1.id, cfg1.last_billed_at, now + timedelta(hours=1)
        )
        assert failed is False


@pytest.mark.asyncio
async def test_config_repo_suspend_unsuspend_all(sessionmaker):
    async with uow() as repos:
        server = await repos["servers"].create(
            name="srv2",
            ip="1.1.1.1",
            port=22,
            host="host",
            location="US",
            api_key="k",
            cost=1,
        )
        user = await repos["users"].get_or_create(5)
        cfg1 = await repos["configs"].create(server.id, user.id, "cfg1", "disp1")
        cfg2 = await repos["configs"].create(server.id, user.id, "cfg2", "disp2")

        suspended = await repos["configs"].suspend_all(user.id)
        assert suspended == 2

        updated1 = await repos["configs"].get(id=cfg1.id)
        updated2 = await repos["configs"].get(id=cfg2.id)
        assert updated1.suspended is True
        assert updated2.suspended is True

        before = datetime.now(timezone.utc).replace(tzinfo=None)
        unsuspended = await repos["configs"].unsuspend_all(user.id)
        assert unsuspended == 2

        updated1 = await repos["configs"].get(id=cfg1.id)
        updated2 = await repos["configs"].get(id=cfg2.id)
        assert updated1.suspended is False
        assert updated2.suspended is False
        assert updated1.last_billed_at >= before
        assert updated2.last_billed_at >= before


@pytest.mark.asyncio
async def test_transaction_repo_ordering_and_pagination(sessionmaker):
    async with uow() as repos:
        user = await repos["users"].get_or_create(4)
        tx1 = await repos["transactions"].create(
            user_id=user.id,
            amount=Decimal("1.00"),
            kind="topup",
            source="test",
        )
        tx2 = await repos["transactions"].create(
            user_id=user.id,
            amount=Decimal("2.00"),
            kind="topup",
            source="test",
        )

        latest = await repos["transactions"].list_for_user(user_id=user.id, limit=1)
        assert latest[0].id == tx2.id

        offset = await repos["transactions"].list_for_user(
            user_id=user.id, limit=1, offset=1
        )
        assert offset[0].id == tx1.id
