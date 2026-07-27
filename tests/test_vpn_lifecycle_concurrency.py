from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import core.db as db
import core.db.unit_of_work as db_uow
import deploy.reconcile_suspended_configs as release_recovery
from core.config import settings
from core.db import Base
from core.db.models.vpn_operation import VPNOperation
from core.db.repo.vpn_operation import VPNOperationRepo
from core.db.unit_of_work import uow
from core.domain import (
    VPNOperationKind,
    VPNOperationStatus,
    VPNState,
)
from core.services import ConfigService, ServerService, UserService
from tests.fleet_test_support import mark_server_ready

POSTGRES_TEST_URL = os.getenv("POSTGRES_TEST_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="POSTGRES_TEST_URL is required for locking/concurrency tests",
)


class _PostgresLifecycleGateway:
    def __init__(self) -> None:
        self.suspend_operation_ids: list[str | None] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def create_client(
        self,
        name,
        use_password=False,
        *,
        operation_id=None,
    ):
        return f"/tmp/{name}.ovpn"

    async def suspend_client(self, name, *, operation_id=None):
        self.suspend_operation_ids.append(operation_id)


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


@pytest.mark.asyncio
async def test_postgres_suspend_completion_persists_aware_timestamp(
    monkeypatch,
):
    engine = create_async_engine(POSTGRES_TEST_URL, echo=False)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    gateway = _PostgresLifecycleGateway()
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)

        monkeypatch.setattr(db_uow, "async_session", maker)
        monkeypatch.setattr(db, "async_session", maker)
        monkeypatch.setattr(settings, "maintenance_mode", False)
        monkeypatch.setattr(settings, "provisioning_enabled", True)
        monkeypatch.setattr(
            "core.services.config.APIGateway",
            lambda *args, **kwargs: gateway,
        )

        user = await UserService(uow).register(912346)
        server = await ServerService(uow).create(
            name="postgres-lifecycle",
            ip="127.0.0.1",
            port=8080,
            host="vpn.test",
            location="local",
            api_key="secret",
            cost=0,
        )
        await mark_server_ready(server.id)
        service = ConfigService(uow)
        config = await service.create_config(
            server_id=server.id,
            owner_id=user.id,
            name="postgres-suspend",
            display_name="PostgreSQL suspend",
        )

        await service.suspend_config(config.id)

        async with uow() as repos:
            row = await repos["configs"].get(id=config.id)
            operation = await repos["vpn_operations"].get(operation_id=row.operation_id)
        assert row.desired_state == VPNState.SUSPENDED.value
        assert row.actual_state == VPNState.SUSPENDED.value
        assert row.suspended is True
        assert row.suspended_at is not None
        assert row.suspended_at.tzinfo is not None
        assert row.suspended_at.utcoffset() == timezone.utc.utcoffset(row.suspended_at)
        assert operation.status == VPNOperationStatus.SUCCEEDED.value
        assert operation.lease_token is None
        assert operation.lease_until is None
        assert len(gateway.suspend_operation_ids) == 1

        assert await service.reconcile() == {}
        assert len(gateway.suspend_operation_ids) == 1
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest.mark.asyncio
async def test_release_recovery_converges_existing_drift_without_ledger_changes(
    monkeypatch,
):
    engine = create_async_engine(POSTGRES_TEST_URL, echo=False)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    gateway = _PostgresLifecycleGateway()
    operation_id = "00000000-0000-4000-9000-000000000099"
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)

        monkeypatch.setattr(db_uow, "async_session", maker)
        monkeypatch.setattr(db, "async_session", maker)
        monkeypatch.setattr(settings, "maintenance_mode", True)
        monkeypatch.setattr(settings, "provisioning_enabled", False)
        monkeypatch.setattr(
            "core.services.config.APIGateway",
            lambda *args, **kwargs: gateway,
        )

        user = await UserService(uow).register(912347, balance="10.00")
        server = await ServerService(uow).create(
            name="postgres-release-recovery",
            ip="127.0.0.1",
            port=8080,
            host="vpn.test",
            location="local",
            api_key="secret",
            cost=0,
        )
        await mark_server_ready(server.id)
        async with uow() as repos:
            config = await repos["configs"].create(
                server.id,
                user.id,
                "postgres-release-suspend",
                "PostgreSQL release suspend",
                desired_state=VPNState.SUSPENDED.value,
                actual_state=VPNState.ACTIVE.value,
                operation_id=operation_id,
            )
            await repos["vpn_operations"].create(
                operation_id=operation_id,
                config_id=config.id,
                config_name=config.name,
                server_id=server.id,
                owner_id=user.id,
                kind=VPNOperationKind.SUSPEND.value,
                next_attempt_at=datetime.now(timezone.utc),
            )
            ledger_before = tuple(
                entry.id
                for entry in await repos["billing"].list_ledger_entries(user.id)
            )

        assert (
            await release_recovery.reconcile_suspended_configs(timeout_seconds=5) == 1
        )

        async with uow() as repos:
            row = await repos["configs"].get(id=config.id)
            operation = await repos["vpn_operations"].get(operation_id=operation_id)
            ledger_after = tuple(
                entry.id
                for entry in await repos["billing"].list_ledger_entries(user.id)
            )
        assert row.actual_state == VPNState.SUSPENDED.value
        assert row.suspended is True
        assert row.suspended_at is not None
        assert operation.status == VPNOperationStatus.SUCCEEDED.value
        assert gateway.suspend_operation_ids == [operation_id]
        assert ledger_after == ledger_before
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()
