import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import core.db as db
from core.db import Base
from core.db.unit_of_work import uow
from core.exceptions import InsufficientBalanceError
from core.services.billing import BillingService
from core.services.server import ServerService
from core.services.user import UserService


pytestmark = pytest.mark.integration


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


def _build_urls(base_url: str) -> tuple[str, str, str]:
    parsed = urlparse(base_url)
    dbname = f"vpn_concurrency_{uuid.uuid4().hex[:8]}"
    admin_url = urlunparse(parsed._replace(path="/postgres"))
    test_url = urlunparse(parsed._replace(path=f"/{dbname}"))
    admin_url = urlunparse(parsed._replace(scheme=parsed.scheme.split("+", 1)[0], path="/postgres"))
    return admin_url, test_url, dbname


@pytest_asyncio.fixture()
async def pg_sessionmaker(monkeypatch):
    if os.getenv("INTEGRATION_TESTS") != "1":
        pytest.skip("Integration tests are disabled")
    base_url = os.getenv("DATABASE_URL", "")
    if not base_url.startswith("postgresql"):
        pytest.skip("Postgres integration tests require a postgres DATABASE_URL")

    admin_url, test_url, dbname = _build_urls(base_url)
    conn = await asyncpg.connect(admin_url)
    await conn.execute(f'CREATE DATABASE "{dbname}"')
    await conn.close()

    engine = create_async_engine(test_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "async_session", maker, raising=False)
    monkeypatch.setattr(db.unit_of_work, "async_session", maker, raising=False)
    try:
        yield maker
    finally:
        await engine.dispose()
        conn = await asyncpg.connect(admin_url)
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1",
            dbname,
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
        await conn.close()


@pytest.mark.asyncio
async def test_concurrent_withdraw_only_one_succeeds(pg_sessionmaker):
    user_svc = UserService(uow)
    billing = BillingService(uow)

    user = await user_svc.register(100)
    await billing.top_up(user.id, 10)

    results = await asyncio.gather(
        billing.withdraw(user.id, 10),
        billing.withdraw(user.id, 10),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]

    assert len(successes) == 1
    assert any(isinstance(f, InsufficientBalanceError) for f in failures)

    updated = await user_svc.get(user.id)
    assert updated.balance == Decimal("0.00")


@pytest.mark.asyncio
async def test_concurrent_create_paid_config_single_charge(monkeypatch, pg_sessionmaker):
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *a, **kw: DummyGateway()
    )

    server_svc = ServerService(uow)
    user_svc = UserService(uow)
    billing = BillingService(uow)
    await billing.update_settings(config_creation_cost=10)

    user = await user_svc.register(101)
    server = await server_svc.create(
        name="srv",
        ip="1.1.1.1",
        port=22,
        host="host",
        location="US",
        api_key="k",
        cost=1,
    )

    await billing.top_up(user.id, 10)

    results = await asyncio.gather(
        billing.create_paid_config(
            server_id=server.id,
            owner_id=user.id,
            name="cfg-a",
            display_name="disp-a",
        ),
        billing.create_paid_config(
            server_id=server.id,
            owner_id=user.id,
            name="cfg-b",
            display_name="disp-b",
        ),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]

    assert len(successes) == 1
    assert any(isinstance(f, InsufficientBalanceError) for f in failures)

    async with uow() as repos:
        cfgs = await repos["configs"].list(owner_id=user.id)
    assert len(cfgs) == 1

    txs = await billing.list_transactions(user_id=user.id)
    creation = [tx for tx in txs if tx.kind == "config_creation"]
    assert len(creation) == 1

    updated = await user_svc.get(user.id)
    assert updated.balance == Decimal("0.00")


@pytest.mark.asyncio
async def test_concurrent_charge_usage_single_billing(pg_sessionmaker):
    user_svc = UserService(uow)
    server_svc = ServerService(uow)
    billing = BillingService(uow)
    await billing.update_settings(monthly_config_cost=720)

    user = await user_svc.register(102)
    server = await server_svc.create(
        name="srv2",
        ip="1.1.1.1",
        port=22,
        host="host",
        location="US",
        api_key="k",
        cost=1,
    )

    await billing.top_up(user.id, 5)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with uow() as repos:
        cfg = await repos["configs"].create(server.id, user.id, "cfg", "disp")
        cfg.last_billed_at = now - timedelta(hours=1)

    results = await asyncio.gather(
        billing.charge_usage(now=now),
        billing.charge_usage(now=now),
    )

    total_charged = sum(
        (sum(result.values(), Decimal("0.00")) for result in results),
        Decimal("0.00"),
    )
    assert total_charged == Decimal("1.00")

    txs = await billing.list_transactions(user_id=user.id)
    usage = [tx for tx in txs if tx.kind == "usage"]
    assert len(usage) == 1

    updated = await user_svc.get(user.id)
    assert updated.balance == Decimal("4.00")
