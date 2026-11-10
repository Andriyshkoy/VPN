"""Real PostgreSQL checks for the new account tariff in the old hourly run."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.db import Base
from core.db.models import BillingRun, LedgerEntry, User
from core.db.models.subscription import Subscription
from core.db.repo.billing import BillingRepo

POSTGRES_TEST_URL = os.getenv("POSTGRES_TEST_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_URL, reason="POSTGRES_TEST_URL required"
)


@pytest.mark.asyncio
async def test_product_hourly_run_and_unlimited_toggle_under_postgres():
    url = make_url(POSTGRES_TEST_URL)
    if not (
        url.drivername == "postgresql+asyncpg"
        and url.host in {"127.0.0.1", "localhost", "::1"}
        and url.database
        in {"vpn_product_core", "vpn_concurrency", "vpn_product_root_tests"}
        and not url.query
    ):
        pytest.fail(
            "Use an allowlisted disposable loopback asyncpg test database without query parameters"
        )
    engine = create_async_engine(POSTGRES_TEST_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 11, 9, tzinfo=timezone.utc)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with maker() as session, session.begin():
            user = User(tg_id=908761, balance=Decimal("100.00"))
            session.add(user)
            await session.flush()
            user_id = user.id
            session.add(
                Subscription(user_id=user_id, plan_code="plus", status="active")
            )

        async def hourly(key, start):
            async with maker() as session, session.begin():
                return await BillingRepo(session).charge_period(
                    period_key=key,
                    period_start=start,
                    period_end=start + timedelta(hours=1),
                    cost_per_config="999.00",
                )

        results = await asyncio.gather(
            *(hourly("product-concurrent", now) for _ in range(12))
        )
        assert sum(len(items) for items in results) == 1
        async with maker() as session:
            assert (await session.get(User, user_id)).balance == Decimal("99.86")
            entries = (await session.scalars(select(LedgerEntry))).all()
            assert len(entries) == 1 and entries[0].amount == Decimal("-0.14")
            assert entries[0].details["pricing_model"] == "subscription_hourly"
            assert (
                await session.scalar(select(func.count()).select_from(BillingRun)) == 1
            )

        locked, release = asyncio.Event(), asyncio.Event()

        async def enable_unlimited():
            async with maker() as session, session.begin():
                user = await session.scalar(
                    select(User).where(User.id == user_id).with_for_update()
                )
                locked.set()
                await release.wait()
                user.unlimited_balance = True

        toggle = asyncio.create_task(enable_unlimited())
        await locked.wait()
        charge = asyncio.create_task(
            hourly("product-concurrent-next", now + timedelta(hours=1))
        )
        await asyncio.sleep(0.03)
        release.set()
        await toggle
        assert await charge == []
        async with maker() as session:
            user = await session.get(User, user_id)
            assert user.unlimited_balance and user.balance == Decimal("99.86")
            assert (
                await session.scalar(select(func.count()).select_from(LedgerEntry)) == 1
            )
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()
