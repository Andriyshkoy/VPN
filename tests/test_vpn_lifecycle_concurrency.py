from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.db import Base
from core.db.models.vpn_operation import VPNOperation
from core.db.repo.vpn_operation import VPNOperationRepo
from core.domain import VPNOperationKind

POSTGRES_TEST_URL = os.getenv("POSTGRES_TEST_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="POSTGRES_TEST_URL is required for locking/concurrency tests",
)


@pytest.mark.asyncio
async def test_only_one_postgres_worker_claims_an_operation():
    engine = create_async_engine(POSTGRES_TEST_URL, echo=False)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    operation_id = "00000000-0000-4000-9000-000000000001"
    now = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)

        async with maker() as session, session.begin():
            session.add(
                VPNOperation(
                    operation_id=operation_id,
                    config_id=None,
                    config_name="concurrent-claim",
                    server_id=None,
                    owner_id=None,
                    kind=VPNOperationKind.SUSPEND.value,
                    next_attempt_at=now,
                )
            )

        async def claim(token: str):
            async with maker() as session, session.begin():
                return await VPNOperationRepo(session).claim(
                    operation_id,
                    lease_token=token,
                    now=now,
                    lease_for=timedelta(minutes=2),
                )

        claims = await asyncio.gather(
            claim("00000000-0000-4000-9000-000000000011"),
            claim("00000000-0000-4000-9000-000000000012"),
        )
        assert sum(result is not None for result in claims) == 1
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()
