"""Upgrade a populated g5 schema without moving money or issuing access."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory


def _upgrade(connection, revisions):
    with Operations.context(MigrationContext.configure(connection)):
        for revision in revisions:
            revision.module.upgrade()


@pytest.mark.asyncio
async def test_populated_g5_upgrade_preserves_financial_and_legacy_access_history():
    raw = os.getenv("POSTGRES_TEST_URL")
    if not raw:
        pytest.skip("POSTGRES_TEST_URL required for the local PostgreSQL upgrade")
    url = make_url(raw)
    assert (
        url.drivername == "postgresql+asyncpg"
        and url.host in {"localhost", "127.0.0.1", "::1"}
        and url.database in {"vpn_concurrency", "vpn_product_root_tests"}
        and not url.query
    ), "Use an explicitly allowed disposable loopback test database"
    schema = f"product_upgrade_test_{uuid4().hex}"
    control = create_async_engine(url, poolclass=NullPool)
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema}},
    )
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.set_main_option(
        "script_location", str(Path(__file__).parents[1] / "alembic")
    )
    scripts = ScriptDirectory.from_config(config)
    legacy_revisions = list(
        reversed(list(scripts.walk_revisions(base="base", head="g5c9d2e3f4a5")))
    )
    created = False
    try:
        async with control.begin() as connection:
            await connection.execute(CreateSchema(schema))
            created = True
        async with engine.begin() as connection:
            await connection.run_sync(_upgrade, legacy_revisions)
            await connection.execute(
                text(
                    'INSERT INTO "user" (id, tg_id, balance, referral_code, '
                    "telegram_delivery_status) VALUES "
                    "(1, 700000001, 123.45, repeat('a', 32), 'active'), "
                    "(2, 700000002, -1.25, repeat('b', 32), 'active'), "
                    "(3, 700000003, 0, repeat('c', 32), 'blocked')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO ledger_entry (user_id, amount, balance_after, kind, "
                    "idempotency_key, details) VALUES "
                    "(1, 123.45, 123.45, 'opening_balance', 'fixture-opening-1', '{}'), "
                    "(2, -1.25, -1.25, 'opening_balance', 'fixture-opening-2', '{}')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO server (id, name, ip, host, location, api_key, "
                    "port, monthly_cost) VALUES "
                    "(1, 'legacy-fixture', '192.0.2.10', 'legacy.example.test', "
                    "'fixture', 'synthetic-not-a-key', 22, 0)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO vpn_config (id, name, server_id, owner_id, "
                    "display_name, suspended, desired_state, actual_state) VALUES "
                    "(1, 'old-active', 1, 1, 'Fixture active', false, 'active', 'active'), "
                    "(2, 'old-suspended', 1, 2, 'Fixture suspended', true, 'suspended', 'suspended')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO billing_run (period_key, period_start, period_end, "
                    "cost_per_config, status, charged_users, total_amount) VALUES "
                    "('fixture-hour-1', '2026-11-01T00:00:00Z', '2026-11-01T01:00:00Z', "
                    "0.07, 'completed', 1, 0.07)"
                )
            )
            tables = ("user", "server", "vpn_config", "ledger_entry", "billing_run")
            columns = await connection.run_sync(
                lambda sync: {
                    table: [item["name"] for item in inspect(sync).get_columns(table)]
                    for table in tables
                }
            )

            async def snapshot():
                return {
                    table: (
                        await connection.execute(
                            text(
                                "SELECT "
                                + ", ".join(f'"{column}"' for column in columns[table])
                                + f' FROM "{table}" ORDER BY id'
                            )
                        )
                    ).all()
                    for table in tables
                }

            before = await snapshot()
            await connection.run_sync(_upgrade, [scripts.get_revision("h6d0e3f4a5b6")])
            assert await snapshot() == before
            assert (
                await connection.execute(
                    text(
                        "SELECT unlimited_balance, exclude_from_business_metrics "
                        'FROM "user" ORDER BY id'
                    )
                )
            ).all() == [(False, False)] * 3
            for table in ("subscription", "subscription_access", "admin_campaign"):
                assert (
                    await connection.scalar(text(f'SELECT count(*) FROM "{table}"'))
                    == 0
                )
    finally:
        await engine.dispose()
        if created:
            async with control.begin() as connection:
                await connection.execute(DropSchema(schema, cascade=True))
        await control.dispose()
