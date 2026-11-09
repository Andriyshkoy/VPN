"""PostgreSQL row-lock tests; each test uses its own disposable schema."""

import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.db import Base
from core.db.models import (
    AdminCampaign,
    CampaignOperation,
    CampaignRecipient,
    NotificationOutbox,
    User,
)
from core.services.campaigns import (
    CampaignConflict,
    CampaignDeliveryService,
    CampaignService,
)

POSTGRES_TEST_URL = os.environ.get("TEST_POSTGRES_URL") or os.environ.get(
    "POSTGRES_TEST_URL"
)
pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_URL, reason="POSTGRES_TEST_URL required for actual row-lock tests"
)


@pytest_asyncio.fixture
async def campaign_pg_maker():
    url = make_url(POSTGRES_TEST_URL)
    if (
        url.host not in {"127.0.0.1", "localhost", "::1"}
        or url.database
        not in {"vpn_concurrency", "vpn_product_admin", "vpn_product_root_tests"}
        or url.drivername != "postgresql+asyncpg"
        or url.query
    ):
        pytest.fail(
            "Campaign locking tests require loopback asyncpg, an explicitly allowed "
            "disposable test database, and no URL query overrides"
        )
    schema = "campaign_test_" + uuid4().hex
    engine = create_async_engine(
        POSTGRES_TEST_URL, connect_args={"server_settings": {"search_path": schema}}
    )
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.run_sync(Base.metadata.create_all)
        yield maker
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def overlap(maker, campaign, *operations, return_exceptions=False):
    """Force both operations to wait for the same campaign lock before release."""
    blocker = maker()
    tasks = []
    engine = maker.kw["bind"].sync_engine
    waiting = asyncio.Event()
    seen = 0

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        nonlocal seen
        if "admin_campaign" in statement and "FOR UPDATE" in statement:
            seen += 1
            if seen >= len(operations):
                waiting.set()

    try:
        await blocker.begin()
        await blocker.execute(
            select(AdminCampaign)
            .where(AdminCampaign.public_id == campaign["id"])
            .with_for_update()
        )
        event.listen(engine, "before_cursor_execute", observe)
        tasks = [asyncio.create_task(operation) for operation in operations]
        await asyncio.wait_for(waiting.wait(), timeout=10)
        await blocker.commit()
        return await asyncio.gather(*tasks, return_exceptions=return_exceptions)
    finally:
        if event.contains(engine, "before_cursor_execute", observe):
            event.remove(engine, "before_cursor_execute", observe)
        await blocker.close()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def prepared(maker):
    async with maker() as session, session.begin():
        session.add_all([User(tg_id=55661), User(tg_id=55662)])
        await session.flush()
        service = CampaignService(session)
        campaign = await service.create(
            name="PG lock test", text="No network delivery", audience={"kind": "all"}
        )
        preview = await service.preview(campaign["id"])
        return campaign, preview


async def launching(maker, campaign, preview, key="same-launch-key"):
    async with maker() as session, session.begin():
        return await CampaignService(session).launch(
            campaign["id"],
            revision=preview["revision"],
            preview_token=preview["preview_token"],
            expected_recipient_count=preview["eligible_count"],
            idempotency_key=key,
        )


async def test_parallel_launch_same_key_has_one_snapshot_and_operation(
    campaign_pg_maker,
):
    maker = campaign_pg_maker
    campaign, preview = await prepared(maker)
    results = await overlap(
        maker,
        campaign,
        launching(maker, campaign, preview),
        launching(maker, campaign, preview),
    )
    assert [r["counts"]["total"] for r in results] == [2, 2]
    async with maker() as session:
        assert await session.scalar(select(func.count(CampaignRecipient.id))) == 2
        assert await session.scalar(select(func.count(NotificationOutbox.id))) == 2
        assert await session.scalar(select(func.count(CampaignOperation.id))) == 1


async def test_parallel_launch_different_keys_returns_conflict_not_integrity_error(
    campaign_pg_maker,
):
    maker = campaign_pg_maker
    campaign, preview = await prepared(maker)
    results = await overlap(
        maker,
        campaign,
        launching(maker, campaign, preview, "launch-first-key"),
        launching(maker, campaign, preview, "launch-second-key"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, CampaignConflict) for result in results) == 1
    async with maker() as session:
        assert await session.scalar(select(func.count(NotificationOutbox.id))) == 2


async def test_parallel_delivery_claim_allows_only_one_attempt(campaign_pg_maker):
    maker = campaign_pg_maker
    campaign, preview = await prepared(maker)
    await launching(maker, campaign, preview)
    async with maker() as session:
        row = await session.scalar(
            select(CampaignRecipient).order_by(CampaignRecipient.id)
        )
    delivery = CampaignDeliveryService(maker)
    claims = await overlap(
        maker,
        campaign,
        *[
            delivery.claim(
                row.outbox_dedupe_key, chat_id=row.tg_id_snapshot, text=campaign["text"]
            )
            for _ in range(2)
        ],
    )
    assert sum(claim.allowed for claim in claims) == 1
    assert any(claim.status == "unknown" for claim in claims)
    async with maker() as session:
        current = await session.get(CampaignRecipient, row.id)
        assert current.attempts == 1 and current.status == "unknown"


async def test_cancel_racing_claim_suppresses_all_unclaimed_work(campaign_pg_maker):
    maker = campaign_pg_maker
    campaign, preview = await prepared(maker)
    await launching(maker, campaign, preview)
    async with maker() as session:
        rows = list(
            (
                await session.scalars(
                    select(CampaignRecipient).order_by(CampaignRecipient.id)
                )
            ).all()
        )
    delivery = CampaignDeliveryService(maker)

    async def cancel():
        async with maker() as session, session.begin():
            return await CampaignService(session).cancel(
                campaign["id"], idempotency_key="racing-cancel-key"
            )

    claim, cancelled = await overlap(
        maker,
        campaign,
        delivery.claim(
            rows[0].outbox_dedupe_key,
            chat_id=rows[0].tg_id_snapshot,
            text=campaign["text"],
        ),
        cancel(),
    )
    assert cancelled["status"] == "cancelled"
    assert not (
        await delivery.claim(
            rows[1].outbox_dedupe_key,
            chat_id=rows[1].tg_id_snapshot,
            text=campaign["text"],
        )
    ).allowed
    if claim.allowed:
        await delivery.finish(rows[0].outbox_dedupe_key, status="delivered")
    async with maker() as session:
        final = await CampaignService(session).get(campaign["id"])
    assert final["status"] == "cancelled"
    assert final["counts"]["cancelled"] == (1 if claim.allowed else 2)
    assert final["counts"]["delivered"] == (1 if claim.allowed else 0)


async def test_parallel_retry_reuses_operation_without_extra_outbox(campaign_pg_maker):
    maker = campaign_pg_maker
    campaign, preview = await prepared(maker)
    await launching(maker, campaign, preview)
    async with maker() as session:
        row = await session.scalar(
            select(CampaignRecipient).order_by(CampaignRecipient.id)
        )
    delivery = CampaignDeliveryService(maker)
    await delivery.claim(
        row.outbox_dedupe_key, chat_id=row.tg_id_snapshot, text=campaign["text"]
    )
    await delivery.finish(
        row.outbox_dedupe_key, status="failed", error="content_failure"
    )

    async def retry():
        async with maker() as session, session.begin():
            return await CampaignService(session).retry_failed(
                campaign["id"], idempotency_key="parallel-retry-key"
            )

    await overlap(maker, campaign, retry(), retry())
    async with maker() as session:
        assert await session.scalar(select(func.count(NotificationOutbox.id))) == 3
        assert (await session.get(CampaignRecipient, row.id)).generation == 1
