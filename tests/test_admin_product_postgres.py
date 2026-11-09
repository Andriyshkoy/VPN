"""Real row-lock races, restricted to a disposable local PostgreSQL database.

Run with TEST_POSTGRES_URL (POSTGRES_TEST_URL is also supported for consistency
with older tests). Each test owns a random schema and never drops public tables.
No application or production database URL is used as a fallback.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

import core.db as db
import core.db.unit_of_work as db_uow
from admin.request_context import RequestContextMiddleware
from admin.routers.admin_v1_subscriptions import users_router
from admin.security import CSRF_COOKIE_NAME, AdminPrincipal, get_admin_principal
from core.db import Base
from core.db.models import AdminAuditEvent, AdminRole, AdminUser, LedgerEntry, User
from core.db.models.subscription import Subscription
from core.services import UserService
from core.services.subscriptions import SubscriptionService

DISPOSABLE_DATABASES = {
    "vpn_concurrency",
    "vpn_product_admin",
    "vpn_product_root_tests",
}


def _disposable_postgres_url(raw):
    url = make_url(raw)
    if (
        url.drivername != "postgresql+asyncpg"
        or url.host not in {"127.0.0.1", "::1", "localhost"}
        or url.database not in DISPOSABLE_DATABASES
        or url.query
    ):
        raise ValueError(
            "PostgreSQL race tests require an explicit loopback asyncpg URL "
            "for an explicitly allowed disposable test database without URL query overrides"
        )
    return url


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "postgresql+asyncpg://test@192.0.2.1:55432/vpn_product_admin",
        "postgresql+asyncpg://test@127.0.0.1:55432/vpn",
        "postgresql+asyncpg://test@localhost:55432/postgres",
        "postgresql+asyncpg://test@localhost/vpn_product_admin?host=192.0.2.1",
        "postgresql+psycopg://test@localhost/vpn_product_admin",
        "sqlite+aiosqlite:///:memory:",
    ],
)
def test_postgres_race_guard_rejects_non_disposable_targets(unsafe_url):
    with pytest.raises(ValueError):
        _disposable_postgres_url(unsafe_url)


@pytest.mark.parametrize("database", sorted(DISPOSABLE_DATABASES))
def test_postgres_race_guard_accepts_explicit_local_test_databases(database):
    url = _disposable_postgres_url(
        f"postgresql+asyncpg://test@127.0.0.1:55432/{database}"
    )
    assert url.database == database


@pytest_asyncio.fixture
async def product_postgres(monkeypatch):
    raw = os.getenv("TEST_POSTGRES_URL") or os.getenv("POSTGRES_TEST_URL")
    if not raw:
        pytest.skip(
            "Set TEST_POSTGRES_URL to an explicitly allowed disposable local DB"
        )
    url = _disposable_postgres_url(raw)
    schema = f"admin_product_test_{uuid4().hex}"
    control = create_async_engine(url, poolclass=NullPool)
    engine = create_async_engine(
        url,
        pool_size=5,
        max_overflow=0,
        connect_args={
            "server_settings": {
                "search_path": schema,
                "statement_timeout": "15000",
                "lock_timeout": "10000",
            }
        },
    )
    schema_created = False
    try:
        async with control.begin() as connection:
            await connection.execute(CreateSchema(schema))
            schema_created = True
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(db, "async_session", maker)
        monkeypatch.setattr(db_uow, "async_session", maker)
        monkeypatch.setattr(db, "engine", engine)
        yield maker, engine
    finally:
        await engine.dispose()
        if schema_created:
            async with control.begin() as connection:
                await connection.execute(DropSchema(schema, cascade=True))
        await control.dispose()


async def _owner(maker):
    async with maker() as session, session.begin():
        actor = AdminUser(
            username="pg-product-owner",
            password_hash="$2b$12$unused-but-never-authenticated",
            role=AdminRole.OWNER.value,
        )
        session.add(actor)
        await session.flush()
    csrf = "postgres-product-csrf-token"
    principal = AdminPrincipal(
        user_id=actor.id,
        username=actor.username,
        role=AdminRole.OWNER,
        session_id=1,
        csrf_token_hash=hashlib.sha256(csrf.encode()).hexdigest(),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    app.include_router(users_router)
    app.dependency_overrides[get_admin_principal] = lambda: principal
    return app, csrf


async def _wallet(maker, user_id):
    async with maker() as session:
        user = await session.get(User, user_id)
        entries = list(
            (
                await session.scalars(
                    select(LedgerEntry)
                    .where(LedgerEntry.user_id == user_id)
                    .order_by(LedgerEntry.id)
                )
            ).all()
        )
        return user.balance, [
            (
                row.id,
                row.amount,
                row.balance_after,
                row.kind,
                row.idempotency_key,
                row.details,
            )
            for row in entries
        ]


async def _overlapping_patches(maker, engine, client, user_id, requests):
    """Hold the account lock until both independent requests attempt to take it."""
    attempted_locks = set()
    both_waiting = asyncio.Event()
    tasks = []

    def before_execute(connection, cursor, statement, parameters, context, many):
        if "FOR UPDATE" in statement.upper():
            attempted_locks.add(id(connection))
            if len(attempted_locks) == len(requests):
                both_waiting.set()

    try:
        async with maker() as blocker, blocker.begin():
            await blocker.scalar(
                select(User).where(User.id == user_id).with_for_update()
            )
            event.listen(engine.sync_engine, "before_cursor_execute", before_execute)
            try:
                tasks = [
                    asyncio.create_task(client.patch(path, headers=headers, json=body))
                    for path, headers, body in requests
                ]
                await asyncio.wait_for(both_waiting.wait(), timeout=5)
                assert all(not task.done() for task in tasks)
            finally:
                event.remove(
                    engine.sync_engine, "before_cursor_execute", before_execute
                )
        return await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _headers(csrf, key):
    return {"X-CSRF-Token": csrf, "Idempotency-Key": key}


def _enable():
    return {
        "enabled": True,
        "expected_enabled": False,
        "reason": "Reviewed local concurrency fixture exemption",
    }


@pytest.mark.asyncio
async def test_postgres_duplicate_unlimited_requests_commit_one_audit_and_one_replay(
    product_postgres,
):
    maker, engine = product_postgres
    target = await UserService(db_uow.uow).register(92_001, balance="23.45")
    before = await _wallet(maker, target.id)
    assert before[1]
    app, csrf = await _owner(maker)
    path = f"/api/admin/v1/users/{target.id}/unlimited-balance"
    request = (path, _headers(csrf, "pg-identical-flag-key-01"), _enable())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://admin.test"
    ) as client:
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        responses = await _overlapping_patches(
            maker, engine, client, target.id, [request, request]
        )
    assert [response.status_code for response in responses] == [200, 200]
    results = [response.json() for response in responses]
    assert sorted(result["replayed"] for result in results) == [False, True]
    assert {**results[0], "replayed": False} == {**results[1], "replayed": False}
    async with maker() as session:
        assert (await session.get(User, target.id)).unlimited_balance is True
        audit = list(
            (
                await session.scalars(
                    select(AdminAuditEvent).where(
                        AdminAuditEvent.action == "user.unlimited_balance_changed"
                    )
                )
            ).all()
        )
        assert len(audit) == 1
        assert audit[0].details["before"] is False
        assert audit[0].details["after"] is True
    assert await _wallet(maker, target.id) == before


@pytest.mark.asyncio
async def test_postgres_competing_unlimited_expected_state_has_single_winner(
    product_postgres,
):
    maker, engine = product_postgres
    target = await UserService(db_uow.uow).register(92_002, balance="34.56")
    before = await _wallet(maker, target.id)
    app, csrf = await _owner(maker)
    path = f"/api/admin/v1/users/{target.id}/unlimited-balance"
    requests = [
        (path, _headers(csrf, f"pg-competing-flag-key-{index}"), _enable())
        for index in (1, 2)
    ]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://admin.test"
    ) as client:
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        responses = await _overlapping_patches(
            maker, engine, client, target.id, requests
        )
    assert sorted(response.status_code for response in responses) == [200, 409]
    async with maker() as session:
        assert (await session.get(User, target.id)).unlimited_balance is True
        audit = list(
            (
                await session.scalars(
                    select(AdminAuditEvent).where(
                        AdminAuditEvent.action == "user.unlimited_balance_changed"
                    )
                )
            ).all()
        )
        assert len(audit) == 1
    assert await _wallet(maker, target.id) == before


@pytest.mark.asyncio
async def test_postgres_plan_change_version_guard_has_single_winner(product_postgres):
    maker, engine = product_postgres
    target = await UserService(db_uow.uow).register(92_003, balance="45.67")
    before = await _wallet(maker, target.id)
    original = await SubscriptionService(db_uow.uow).select_plan(target.id, "basic")
    app, csrf = await _owner(maker)
    path = f"/api/admin/v1/users/{target.id}/subscription"
    body = {
        "plan_code": "plus",
        "expected_version": original.version,
        "reason": "Reviewed concurrent local tariff change",
    }
    requests = [
        (path, _headers(csrf, f"pg-competing-plan-key-{index}"), body)
        for index in (1, 2)
    ]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://admin.test"
    ) as client:
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        responses = await _overlapping_patches(
            maker, engine, client, target.id, requests
        )
    assert sorted(response.status_code for response in responses) == [200, 409]
    async with maker() as session:
        subscription = await session.scalar(
            select(Subscription).where(Subscription.user_id == target.id)
        )
        assert subscription.plan_code == "plus"
        assert subscription.version == original.version + 1
        audit = list(
            (
                await session.scalars(
                    select(AdminAuditEvent).where(
                        AdminAuditEvent.action == "subscription.plan_changed"
                    )
                )
            ).all()
        )
        assert len(audit) == 1
        assert (await session.get(User, target.id)).unlimited_balance is False
    assert await _wallet(maker, target.id) == before
