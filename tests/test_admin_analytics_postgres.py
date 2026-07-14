from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import core.db as db
import core.db.unit_of_work as db_uow
from core.db import Base
from core.db.models import User
from core.services.admin_queries import (
    AdminAnalyticsQueryService,
    AdminUserQueryService,
)

POSTGRES_TEST_URL = os.getenv("POSTGRES_TEST_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="POSTGRES_TEST_URL is required for PostgreSQL timestamp tests",
)


@pytest.mark.asyncio
async def test_aware_admin_periods_support_legacy_naive_user_timestamp(monkeypatch):
    engine = create_async_engine(POSTGRES_TEST_URL, echo=False)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "async_session", maker)
    monkeypatch.setattr(db_uow, "async_session", maker)
    now = datetime.now(timezone.utc)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with maker() as session, session.begin():
            session.add(
                User(
                    tg_id=900_000_001,
                    username="postgres-period-user",
                    referral_code="postgres-period-user",
                    created=now.replace(tzinfo=None),
                )
            )

        analytics = AdminAnalyticsQueryService(db_uow.uow)
        dashboard = await analytics.dashboard(
            period_from=now - timedelta(days=1),
            period_to=now + timedelta(days=1),
        )
        assert dashboard["users"]["new"] == 1

        users = AdminUserQueryService(db_uow.uow)
        page = await users.list_users(
            created_from=now - timedelta(days=1),
            created_to=now + timedelta(days=1),
        )
        assert page["total"] == 1
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()
