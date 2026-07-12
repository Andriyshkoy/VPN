from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.db import Base
from core.db.models.telegram_update import TelegramUpdateInbox
from core.db.repo.telegram_update import TelegramUpdateRepo

POSTGRES_TEST_URL = os.getenv("POSTGRES_TEST_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="POSTGRES_TEST_URL is required for locking/concurrency tests",
)


@pytest.mark.asyncio
async def test_postgres_deduplicates_ingestion_and_grants_one_processing_lease():
    engine = create_async_engine(POSTGRES_TEST_URL, echo=False)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)

        async def ingest(payload: dict):
            async with maker() as session, session.begin():
                return await TelegramUpdateRepo(session).ingest(
                    update_id=800,
                    payload=payload,
                    source="polling",
                    ordering_key="v1:test-lane",
                )

        ingestions = await asyncio.gather(
            ingest({"update_id": 800, "message": {"text": "one"}}),
            ingest({"update_id": 800, "message": {"text": "two"}}),
        )
        assert sorted(created for _, created in ingestions) == [False, True]
        async with maker() as session:
            count = await session.scalar(
                select(func.count()).select_from(TelegramUpdateInbox)
            )
            assert count == 1

        async def claim(token: str):
            async with maker() as session, session.begin():
                return await TelegramUpdateRepo(session).claim_next(
                    lease_token=token,
                    now=now,
                    lease_for=timedelta(minutes=5),
                    max_attempts=20,
                )

        claims = await asyncio.gather(
            claim("00000000-0000-4000-9000-000000000001"),
            claim("00000000-0000-4000-9000-000000000002"),
        )
        assert sum(result is not None for result in claims) == 1
        winner = next(result for result in claims if result is not None)
        async with maker() as session, session.begin():
            assert await TelegramUpdateRepo(session).mark_processed(
                800,
                lease_token=winner.lease_token,
                now=now,
            )

        async def insert(update_id: int, lane: str):
            async with maker() as session, session.begin():
                return await TelegramUpdateRepo(session).ingest(
                    update_id=update_id,
                    payload={"update_id": update_id},
                    source="polling",
                    ordering_key=lane,
                )

        await asyncio.gather(
            insert(801, "v1:lane-a"),
            insert(802, "v1:lane-a"),
            insert(803, "v1:lane-b"),
        )
        async with maker() as session, session.begin():
            lane_a = await TelegramUpdateRepo(session).claim_next(
                lease_token="00000000-0000-4000-9000-000000000021",
                now=now,
                lease_for=timedelta(minutes=5),
                max_attempts=20,
            )
        assert lane_a is not None and lane_a.update_id == 801
        async with maker() as session, session.begin():
            assert await TelegramUpdateRepo(session).mark_failed(
                801,
                lease_token=lane_a.lease_token,
                error="retry lane a",
                now=now,
                next_attempt_at=now + timedelta(minutes=1),
                exhausted=False,
            )
        async with maker() as session, session.begin():
            other_lane = await TelegramUpdateRepo(session).claim_next(
                lease_token="00000000-0000-4000-9000-000000000022",
                now=now,
                lease_for=timedelta(minutes=5),
                max_attempts=20,
            )
        assert other_lane is not None and other_lane.update_id == 803
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()
