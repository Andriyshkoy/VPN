from __future__ import annotations

import importlib.util
from pathlib import Path

from core.db.models.config import VPN_Config

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "f4b8c1d2e3a4_vpn_suspension_timestamp_timezone.py"
)


def _migration_module():
    spec = importlib.util.spec_from_file_location(
        "vpn_suspension_timestamp_timezone_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_suspension_timestamp_metadata_is_timezone_aware():
    column = VPN_Config.__table__.c.suspended_at
    assert column.type.timezone is True


def test_suspension_timestamp_migration_preserves_utc_instants():
    migration = _migration_module()
    assert migration.down_revision == "e9f1a2b3c4d5"
    calls: list[tuple[tuple, dict]] = []

    class Dialect:
        name = "postgresql"

    class Bind:
        dialect = Dialect()

    class FakeOperations:
        @staticmethod
        def get_bind():
            return Bind()

        @staticmethod
        def alter_column(*args, **kwargs):
            calls.append((args, kwargs))

    migration.op = FakeOperations()
    migration.upgrade()
    migration.downgrade()

    assert [call[0] for call in calls] == [
        ("vpn_config", "suspended_at"),
        ("vpn_config", "suspended_at"),
    ]
    assert calls[0][1]["existing_type"].timezone is False
    assert calls[0][1]["type_"].timezone is True
    assert calls[0][1]["postgresql_using"] == "suspended_at AT TIME ZONE 'UTC'"
    assert calls[1][1]["existing_type"].timezone is True
    assert calls[1][1]["type_"].timezone is False
    assert calls[1][1]["postgresql_using"] == "suspended_at AT TIME ZONE 'UTC'"


def test_suspension_timestamp_migration_is_noop_outside_postgres():
    migration = _migration_module()
    calls: list[tuple[tuple, dict]] = []

    class Dialect:
        name = "sqlite"

    class Bind:
        dialect = Dialect()

    class FakeOperations:
        @staticmethod
        def get_bind():
            return Bind()

        @staticmethod
        def alter_column(*args, **kwargs):
            calls.append((args, kwargs))

    migration.op = FakeOperations()
    migration.upgrade()
    migration.downgrade()

    assert calls == []
