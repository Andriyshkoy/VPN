from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

import core.db as db
from admin.fleet_schemas import AdminServerActionRequest
from admin.fleet_service import AdminFleetService, FleetIdempotencyConflict
from admin.security import AdminPrincipal, AdminRole
from core.db import Base
from core.db.models import AdminAction, AdminUser, Server

POSTGRES_TEST_URL = os.getenv("POSTGRES_TEST_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="POSTGRES_TEST_URL is required for locking/concurrency tests",
)


def _request(server_id: int) -> Request:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": f"/api/admin/v1/servers/{server_id}/actions",
            "headers": [(b"host", b"admin.test")],
            "client": ("127.0.0.1", 10000 + server_id),
            "server": ("admin.test", 443),
        }
    )
    request.state.request_id = f"concurrent-{server_id}"
    request.state.correlation_id = "concurrent-fleet-action"
    return request


@pytest.mark.asyncio
async def test_cross_server_idempotency_race_returns_conflict_not_integrity_error(
    monkeypatch,
):
    engine = create_async_engine(POSTGRES_TEST_URL, echo=False)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "async_session", maker)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        async with maker() as session, session.begin():
            admin = AdminUser(
                username="fleet-concurrency",
                password_hash="unused",
                role=AdminRole.OWNER.value,
            )
            first = Server(
                name="first",
                ip="manager-1.test",
                port=16290,
                host="vpn-1.test",
                location="NL",
                api_key="secret-1",
                monthly_cost=0,
            )
            second = Server(
                name="second",
                ip="manager-2.test",
                port=16290,
                host="vpn-2.test",
                location="DE",
                api_key="secret-2",
                monthly_cost=0,
            )
            session.add_all((admin, first, second))
            await session.flush()
            admin_id, first_id, second_id = admin.id, first.id, second.id
        principal = AdminPrincipal(
            user_id=admin_id,
            username="fleet-concurrency",
            role=AdminRole.OWNER,
            session_id=1,
            csrf_token_hash="x" * 64,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        service = AdminFleetService()

        async def drain(server_id: int):
            return await service.execute_action(
                server_id,
                request=_request(server_id),
                principal=principal,
                client_key="same-key-across-two-servers",
                command=AdminServerActionRequest(
                    action="drain",
                    reason=f"drain server {server_id}",
                    expected_version=1,
                ),
            )

        results = await asyncio.gather(
            drain(first_id), drain(second_id), return_exceptions=True
        )
        assert (
            sum(isinstance(result, FleetIdempotencyConflict) for result in results) == 1
        )
        assert sum(isinstance(result, dict) for result in results) == 1
        assert not any(type(result).__name__ == "IntegrityError" for result in results)
        async with maker() as session:
            assert await session.scalar(select(func.count(AdminAction.id))) == 1
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()
