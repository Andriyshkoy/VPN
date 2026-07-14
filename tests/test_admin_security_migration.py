from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "a6b4c2d8e901_admin_security_foundation.py"
)


def _migration_module():
    spec = importlib.util.spec_from_file_location(
        "admin_security_foundation_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_admin_security_migration_upgrades_and_downgrades_sqlite():
    migration = _migration_module()
    assert migration.down_revision == "f1a8c3d9e742"
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        inspector = sa.inspect(connection)
        assert {"admin_user", "admin_session", "admin_audit_event"}.issubset(
            inspector.get_table_names()
        )
        admin_user_checks = {
            check["name"] for check in inspector.get_check_constraints("admin_user")
        }
        assert "ck_admin_user_role" in admin_user_checks
        assert "ck_admin_user_normalized_username" in admin_user_checks
        assert {
            index["name"] for index in inspector.get_indexes("admin_audit_event")
        } >= {
            "ix_admin_audit_event_action",
            "ix_admin_audit_event_request_id",
            "ix_admin_audit_event_correlation_id",
        }

        migration.downgrade()
        assert not {
            "admin_user",
            "admin_session",
            "admin_audit_event",
        }.intersection(sa.inspect(connection).get_table_names())
    engine.dispose()


def test_postgres_migration_installs_update_delete_and_truncate_guards():
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
    migration._install_admin_audit_immutability_guard()
    sql = "\n".join(executed)
    assert "BEFORE UPDATE OR DELETE ON admin_audit_event" in sql
    assert "BEFORE TRUNCATE ON admin_audit_event" in sql
    assert "rows are immutable" in sql


def test_migration_contains_no_bootstrap_credentials():
    source = MIGRATION_PATH.read_text()
    assert "ADMIN_PASSWORD" not in source
    assert "$2b$" not in source
