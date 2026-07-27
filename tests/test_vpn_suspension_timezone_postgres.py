from __future__ import annotations

import importlib.util
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic.migration import MigrationContext
from alembic.operations import Operations

POSTGRES_TEST_URL = os.getenv("POSTGRES_TEST_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="POSTGRES_TEST_URL is required for PostgreSQL migration tests",
)

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "f4b8c1d2e3a4_vpn_suspension_timestamp_timezone.py"
)


def _migration_module():
    spec = importlib.util.spec_from_file_location(
        "vpn_suspension_timestamp_timezone_postgres_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration(connection, direction: str) -> None:
    migration = _migration_module()
    migration.op = Operations(MigrationContext.configure(connection))
    getattr(migration, direction)()


@pytest.mark.asyncio
async def test_postgres_migration_preserves_legacy_utc_instant():
    engine = create_async_engine(POSTGRES_TEST_URL, echo=False)
    legacy = datetime(2026, 7, 18, 8, 0, 56)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DROP TABLE IF EXISTS vpn_config CASCADE"))
            await connection.execute(
                text(
                    "CREATE TABLE vpn_config ("
                    "id INTEGER PRIMARY KEY, "
                    "suspended_at TIMESTAMP WITHOUT TIME ZONE NULL"
                    ")"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO vpn_config (id, suspended_at) "
                    "VALUES (1, :suspended_at)"
                ),
                {"suspended_at": legacy},
            )

            await connection.run_sync(_run_migration, "upgrade")
            upgraded_type = await connection.scalar(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'vpn_config' "
                    "AND column_name = 'suspended_at'"
                )
            )
            upgraded = await connection.scalar(
                text("SELECT suspended_at FROM vpn_config WHERE id = 1")
            )

            assert upgraded_type == "timestamp with time zone"
            assert upgraded == legacy.replace(tzinfo=timezone.utc)

            await connection.run_sync(_run_migration, "downgrade")
            downgraded_type = await connection.scalar(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'vpn_config' "
                    "AND column_name = 'suspended_at'"
                )
            )
            downgraded = await connection.scalar(
                text("SELECT suspended_at FROM vpn_config WHERE id = 1")
            )

            assert downgraded_type == "timestamp without time zone"
            assert downgraded == legacy
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DROP TABLE IF EXISTS vpn_config CASCADE"))
        await engine.dispose()
