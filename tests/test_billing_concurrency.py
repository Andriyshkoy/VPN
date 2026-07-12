from __future__ import annotations

import asyncio
import os
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.db import Base
from core.db.models.ledger import LedgerEntry, LedgerKind
from core.db.models.user import User
from core.db.repo.billing import BillingRepo

POSTGRES_TEST_URL = os.getenv("POSTGRES_TEST_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="POSTGRES_TEST_URL is required for locking/concurrency tests",
)


@pytest.mark.asyncio
async def test_atomic_updates_and_duplicate_key_under_postgres():
    engine = create_async_engine(POSTGRES_TEST_URL, echo=False)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)

        async with maker() as session, session.begin():
            db_user = User(tg_id=9001, balance=Decimal("0.00"))
            session.add(db_user)
            await session.flush()
            user_id = db_user.id

        async def credit(key: str):
            async with maker() as session, session.begin():
                return await BillingRepo(session).apply_balance_change(
                    user_id=user_id,
                    amount="0.10",
                    kind=LedgerKind.MANUAL_TOP_UP,
                    idempotency_key=key,
                    allow_negative_balance=True,
                )

        # Different operations must not lose either increment while contending
        # for the same user row.
        await asyncio.gather(credit("concurrent:a"), credit("concurrent:b"))
        async with maker() as session:
            assert (await session.get(User, user_id)).balance == Decimal("0.20")

        # Duplicate delivery may race, but only one transaction may change the
        # balance and append the unique ledger record.
        results = await asyncio.gather(
            credit("concurrent:duplicate"), credit("concurrent:duplicate")
        )
        assert sorted(result.applied for result in results) == [False, True]
        async with maker() as session:
            assert (await session.get(User, user_id)).balance == Decimal("0.30")
            count = await session.scalar(
                select(func.count())
                .select_from(LedgerEntry)
                .where(LedgerEntry.user_id == user_id)
            )
            assert count == 3
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()
