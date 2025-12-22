import asyncio
import os
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest


pytestmark = pytest.mark.integration


def _get_base_url() -> str:
    base_url = os.getenv("DATABASE_URL")
    if not base_url:
        pytest.skip("DATABASE_URL not set")
    if not base_url.startswith("postgresql"):
        pytest.skip("Postgres integration tests require a postgres DATABASE_URL")
    return base_url


def _build_urls(base_url: str) -> tuple[str, str, str]:
    parsed = urlparse(base_url)
    dbname = f"vpn_test_{uuid.uuid4().hex[:8]}"
    admin_url = urlunparse(
        parsed._replace(scheme=parsed.scheme.split("+", 1)[0], path="/postgres")
    )
    test_url = urlunparse(parsed._replace(path=f"/{dbname}"))
    test_asyncpg_url = urlunparse(
        parsed._replace(scheme=parsed.scheme.split("+", 1)[0], path=f"/{dbname}")
    )
    return admin_url, test_url, test_asyncpg_url, dbname


async def _create_db(admin_url: str, dbname: str) -> None:
    conn = await asyncpg.connect(admin_url)
    await conn.execute(f'CREATE DATABASE "{dbname}"')
    await conn.close()


async def _drop_db(admin_url: str, dbname: str) -> None:
    conn = await asyncpg.connect(admin_url)
    await conn.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1",
        dbname,
    )
    await conn.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
    await conn.close()


async def _schema_has_column(conn, table: str, column: str) -> bool:
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name=$1",
        table,
    )
    return column in {row["column_name"] for row in rows}


async def _assert_schema(db_url: str, *, expect_referral: bool) -> None:
    conn = await asyncpg.connect(db_url)

    row = await conn.fetchrow("SELECT id FROM billing_settings WHERE id = 1")
    assert row is not None

    has_referral_col = await _schema_has_column(
        conn, "billing_settings", "referral_first_deposit_bonus_pct"
    )
    has_related_user = await _schema_has_column(
        conn, "balance_transaction", "related_user_id"
    )
    has_referral_flag = await _schema_has_column(
        conn, "user", "referral_first_bonus_paid"
    )

    assert has_referral_col is expect_referral
    assert has_related_user is expect_referral
    assert has_referral_flag is expect_referral

    await conn.close()


def test_alembic_upgrade_and_downgrade_roundtrip():
    if os.getenv("INTEGRATION_TESTS") != "1":
        pytest.skip("Integration tests are disabled")

    base_url = _get_base_url()
    admin_url, test_url, test_asyncpg_url, dbname = _build_urls(base_url)
    repo_root = Path(__file__).resolve().parents[1]

    asyncio.run(_create_db(admin_url, dbname))
    try:
        env = os.environ.copy()
        env["DATABASE_URL"] = test_url
        env.setdefault(
            "ENCRYPTION_KEY",
            "KeooZFUkuoYlZe6Ic0zPPC_W-s5UgC2vT2dcWbRjL3Y=",
        )
        env["PYTHONPATH"] = str(repo_root)

        subprocess.run(
            ["alembic", "upgrade", "head"],
            cwd=repo_root,
            env=env,
            check=True,
        )
        asyncio.run(_assert_schema(test_asyncpg_url, expect_referral=True))

        subprocess.run(
            ["alembic", "downgrade", "-1"],
            cwd=repo_root,
            env=env,
            check=True,
        )
        asyncio.run(_assert_schema(test_asyncpg_url, expect_referral=False))

        subprocess.run(
            ["alembic", "upgrade", "head"],
            cwd=repo_root,
            env=env,
            check=True,
        )
        asyncio.run(_assert_schema(test_asyncpg_url, expect_referral=True))
    finally:
        asyncio.run(_drop_db(admin_url, dbname))
