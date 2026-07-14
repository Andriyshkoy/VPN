from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "e9f1a2b3c4d5_telegram_user_action_timeline.py"
)


def _migration_module():
    spec = importlib.util.spec_from_file_location(
        "telegram_user_action_timeline_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_timeline_migration_round_trips_and_enforces_idempotency_sqlite():
    migration = _migration_module()
    assert migration.down_revision == "d4e7f9a1b2c3"
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    user = sa.Table(
        "user",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(user.insert().values(id=1))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        inspector = sa.inspect(connection)
        assert "telegram_user_action_event" in inspector.get_table_names()
        assert {
            "ix_telegram_user_action_user_occurred",
            "ix_telegram_user_action_event_action",
            "ix_telegram_user_action_event_result",
        }.issubset(
            {
                index["name"]
                for index in inspector.get_indexes("telegram_user_action_event")
            }
        )
        action = sa.table(
            "telegram_user_action_event",
            sa.column("id"),
            sa.column("user_id"),
            sa.column("source_update_id"),
            sa.column("category"),
            sa.column("action"),
            sa.column("result"),
            sa.column("metadata", sa.JSON()),
        )
        connection.execute(
            sa.insert(action).values(
                id=1,
                user_id=1,
                source_update_id=100,
                category="bot",
                action="navigation.start",
                result="handled",
                metadata={},
            )
        )
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.insert(action).values(
                        id=2,
                        user_id=1,
                        source_update_id=100,
                        category="bot",
                        action="navigation.start",
                        result="handled",
                        metadata={},
                    )
                )
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    sa.insert(action).values(
                        id=3,
                        user_id=1,
                        source_update_id=101,
                        category="bot",
                        action="navigation.start",
                        result="processed",
                        metadata={},
                    )
                )

        migration.downgrade()
        assert (
            "telegram_user_action_event" not in sa.inspect(connection).get_table_names()
        )
    engine.dispose()


def test_postgres_migration_installs_all_immutability_guards():
    migration = _migration_module()
    executed: list[str] = []

    class Dialect:
        name = "postgresql"

    class Bind:
        dialect = Dialect()

    class FakeOperations:
        @staticmethod
        def get_bind():
            return Bind()

        @staticmethod
        def execute(statement):
            executed.append(str(statement))

    migration.op = FakeOperations()
    migration._install_immutability_guard()
    sql = "\n".join(executed)
    assert "BEFORE UPDATE OR DELETE ON telegram_user_action_event" in sql
    assert "BEFORE TRUNCATE ON telegram_user_action_event" in sql
    assert "rows are immutable" in sql


def test_timeline_migration_contains_no_secrets_or_backfilled_raw_updates():
    source = MIGRATION_PATH.read_text()
    assert "telegram_update_inbox" not in source
    assert "BOT_TOKEN" not in source
    assert "password_hash" not in source
