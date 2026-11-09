from __future__ import annotations

import importlib.util
import io
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations
from core.db.repo import ConfigRepo, ServerRepo, UserRepo, VPNAccessBindingRepo

MIGRATION = (
    Path(__file__).parents[1]
    / "alembic/versions/g5c9d2e3f4a5_dual_transport_node_bindings.py"
)


def migration_module():
    spec = importlib.util.spec_from_file_location("node_binding_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_node_migration_preserves_legacy_finance_and_binding_after_deletion():
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    with engine.connect() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys=ON"))
        connection.commit()
        with connection.begin():
            connection.execute(
                sa.text(
                    "CREATE TABLE server (id INTEGER PRIMARY KEY, name TEXT, api_key TEXT)"
                )
            )
            connection.execute(
                sa.text(
                    "CREATE TABLE vpn_config (id INTEGER PRIMARY KEY, name TEXT, server_id INTEGER REFERENCES server(id))"
                )
            )
            connection.execute(
                sa.text(
                    "CREATE TABLE user (id INTEGER PRIMARY KEY, balance NUMERIC(18,2))"
                )
            )
            connection.execute(
                sa.text(
                    "CREATE TABLE ledger_entry (id INTEGER PRIMARY KEY, amount NUMERIC(18,2), idempotency_key TEXT)"
                )
            )
            connection.execute(
                sa.text("INSERT INTO server VALUES (1,'legacy','encrypted-secret')")
            )
            connection.execute(
                sa.text("INSERT INTO vpn_config VALUES (1,'existing-config',1)")
            )
            connection.execute(sa.text("INSERT INTO user VALUES (1,135.10)"))
            connection.execute(
                sa.text("INSERT INTO ledger_entry VALUES (1,22.00,'old-credit')")
            )
            finance_before = (
                connection.execute(sa.text("SELECT * FROM user")).all(),
                connection.execute(sa.text("SELECT * FROM ledger_entry")).all(),
            )
            migration = migration_module()
            assert migration.down_revision == "f4b8c1d2e3a4"
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            legacy = connection.execute(
                sa.text(
                    "SELECT backend_type,node_id,api_key,endpoint_revision FROM server"
                )
            ).one()
            assert legacy == ("openvpn-manager", None, "encrypted-secret", 1)
            with pytest.raises(sa.exc.IntegrityError):
                with connection.begin_nested():
                    connection.execute(sa.text("UPDATE server SET endpoint_revision=0"))
            assert connection.execute(
                sa.text("SELECT name,service_id,access_policy FROM vpn_config")
            ).one() == ("existing-config", None, "{}")
            remote_id, operation_id = str(uuid4()), str(uuid4())
            connection.execute(
                sa.text(
                    """
                INSERT INTO vpn_access_binding
                    (config_id,config_ref,server_id,node_id,service_id,provision_operation_id,remote_access_id,observed_state)
                VALUES (1,1,1,'nl-1','hysteria',:op,:remote,'revoked')
            """
                ),
                {"op": operation_id, "remote": remote_id},
            )
            connection.execute(sa.text("DELETE FROM vpn_config WHERE id=1"))
            connection.execute(sa.text("DELETE FROM server WHERE id=1"))
            assert connection.execute(
                sa.text(
                    "SELECT config_id,config_ref,server_id,node_id,remote_access_id FROM vpn_access_binding"
                )
            ).one() == (None, 1, None, "nl-1", remote_id)
            with pytest.raises(RuntimeError, match="durable node bindings"):
                migration.downgrade()
            assert (
                connection.execute(sa.text("SELECT * FROM user")).all(),
                connection.execute(sa.text("SELECT * FROM ledger_entry")).all(),
            ) == finance_before
            connection.execute(sa.text("DELETE FROM vpn_access_binding"))
            migration.downgrade()
            assert "vpn_access_binding" not in sa.inspect(connection).get_table_names()
            assert "backend_type" not in {
                c["name"] for c in sa.inspect(connection).get_columns("server")
            }
            assert "service_id" not in {
                c["name"] for c in sa.inspect(connection).get_columns("vpn_config")
            }


def test_node_migration_emits_postgres_defaults_and_identity_constraints():
    output = io.StringIO()
    migration = migration_module()
    migration.op = Operations(
        MigrationContext.configure(
            dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
        )
    )
    migration.upgrade()
    sql = output.getvalue()
    assert "DEFAULT 'openvpn-manager' NOT NULL" in sql
    assert "uq_vpn_binding_remote_access UNIQUE (node_id, remote_access_id)" in sql
    assert "ON DELETE SET NULL" in sql
    assert "TIMESTAMP WITH TIME ZONE" in sql
    assert "ck_server_positive_endpoint_revision CHECK (endpoint_revision >= 1)" in sql
    assert "ledger_entry" not in sql


async def make_config(session, name="node-test"):
    user = await UserRepo(session).get_or_create(42)
    server = await ServerRepo(session).create(
        name="NL",
        ip="127.0.0.1",
        port=18010,
        host="node",
        location="NL",
        api_key="test-node-token",
        cost=0,
        backend_type="node-agent",
        node_id="nl-1",
    )
    config = await ConfigRepo(session).create(
        server.id,
        user.id,
        name,
        name,
        service_id="hysteria",
        access_policy={"activation_days": 7, "traffic_limit_bytes": 0},
    )
    return server, config


@pytest.mark.asyncio
async def test_binding_replay_is_immutable_and_remote_id_is_write_once(session):
    server, config = await make_config(session)
    repo = VPNAccessBindingRepo(session)
    operation = str(uuid4())
    policy = {"activation_days": 7, "traffic_limit_bytes": 0}
    args = dict(
        config_id=config.id,
        server_id=server.id,
        node_id="nl-1",
        service_id="hysteria",
        provision_operation_id=operation,
        policy=policy,
    )
    binding = await repo.create(**args)
    policy["activation_days"] = 99
    assert binding.policy["activation_days"] == 7
    args["policy"] = {"activation_days": 7, "traffic_limit_bytes": 0}
    assert (await repo.create(**args)).id == binding.id
    with pytest.raises(ValueError, match="immutable"):
        await repo.create(**{**args, "policy": {"activation_days": 8}})
    remote_id = str(uuid4())
    assert (
        await repo.attach_remote(config.id, remote_id)
    ).remote_access_id == remote_id
    assert (await repo.attach_remote(config.id, remote_id)).id == binding.id
    with pytest.raises(ValueError, match="immutable"):
        await repo.attach_remote(config.id, str(uuid4()))
    await repo.update_observed(config.id, "limited")
    assert binding.observed_state == "limited"
    await repo.tombstone(config.id)
    assert binding.observed_state == "revoked" and binding.revoked_at is not None
    with pytest.raises(ValueError, match="revoked"):
        await repo.attach_remote(config.id, remote_id)
    with pytest.raises(ValueError, match="revoked"):
        await repo.update_observed(config.id, "active")


@pytest.mark.asyncio
async def test_bindings_do_not_reserve_a_remote_uuid_before_confirmation(session):
    server, config = await make_config(session)
    second = await ConfigRepo(session).create(
        server.id,
        config.owner_id,
        "second",
        "Second",
        service_id="vless-reality",
        access_policy={},
    )
    repo = VPNAccessBindingRepo(session)
    for cfg, service in ((config, "hysteria"), (second, "vless-reality")):
        binding = await repo.create(
            config_id=cfg.id,
            server_id=server.id,
            node_id="nl-1",
            service_id=service,
            provision_operation_id=str(uuid4()),
            policy={},
        )
        assert binding.remote_access_id is None
    assert len(await repo.list(node_id="nl-1")) == 2
