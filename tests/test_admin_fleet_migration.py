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
    / "d4e7f9a1b2c3_admin_fleet_management.py"
)


def _migration_module():
    spec = importlib.util.spec_from_file_location(
        "admin_fleet_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fleet_migration_preserves_servers_and_round_trips_sqlite():
    migration = _migration_module()
    assert migration.down_revision == "a6b4c2d8e901"
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    server = sa.Table(
        "server",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("ip", sa.String(64), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("host", sa.String(128), nullable=False),
        sa.Column("monthly_cost", sa.Numeric(10, 2), nullable=False),
        sa.Column("location", sa.String(128), nullable=False),
        sa.Column("api_key", sa.String(), nullable=False),
    )
    sa.Table(
        "admin_user",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            server.insert().values(
                id=1,
                name="existing",
                ip="10.77.77.2",
                port=16290,
                host="vpn.example",
                monthly_cost=0,
                location="NL",
                api_key="encrypted",
            )
        )
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()

        inspector = sa.inspect(connection)
        assert {"vpn_server_status", "admin_action"}.issubset(
            inspector.get_table_names()
        )
        columns = {column["name"] for column in inspector.get_columns("server")}
        assert {
            "lifecycle_state",
            "accepts_new_configs",
            "max_configs",
            "capacity_reserve",
            "placement_weight",
            "provider",
            "public_endpoint",
            "manager_instance_id",
            "version",
        }.issubset(columns)
        row = connection.execute(
            sa.text(
                "SELECT name, lifecycle_state, accepts_new_configs, version "
                "FROM server WHERE id = 1"
            )
        ).one()
        assert row.name == "existing"
        assert row.lifecycle_state == "active"
        assert bool(row.accepts_new_configs) is True
        assert row.version == 1

        migration.downgrade()
        inspector = sa.inspect(connection)
        assert not {"vpn_server_status", "admin_action"}.intersection(
            inspector.get_table_names()
        )
        remaining = {column["name"] for column in inspector.get_columns("server")}
        assert "lifecycle_state" not in remaining
        assert connection.scalar(sa.text("SELECT count(*) FROM server")) == 1
    engine.dispose()


def test_fleet_migration_contains_no_credentials_or_remote_execution():
    source = MIGRATION_PATH.read_text()
    assert "API_KEY=" not in source
    assert "subprocess" not in source
    assert "ssh" not in source.casefold()
