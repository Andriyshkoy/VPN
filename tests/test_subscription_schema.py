"""Exercise the irreversible boundaries of the prepared product schema."""

import importlib.util
import io
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from core.db import Base

REVISION_PATH = (
    Path(__file__).parents[1]
    / "alembic/versions/h6d0e3f4a5b6_subscriptions_and_reviewed_campaigns.py"
)
NEW_TABLES = {
    "subscription",
    "subscription_access",
    "admin_campaign",
    "campaign_recipient",
    "campaign_operation",
}


def _revision():
    spec = importlib.util.spec_from_file_location(
        "prepared_product_revision", REVISION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def migrated_connection():
    """An isolated legacy schema with real preexisting monetary history."""
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql(
            'CREATE TABLE "user" (id INTEGER PRIMARY KEY, balance NUMERIC(18, 2) NOT NULL)'
        )
        connection.exec_driver_sql(
            "CREATE TABLE ledger_entry (id INTEGER PRIMARY KEY, user_id INTEGER, "
            "amount NUMERIC(18, 2), balance_after NUMERIC(18, 2), kind TEXT)"
        )
        for table in ("vpn_config", "admin_user", "notification_outbox"):
            connection.exec_driver_sql(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql(
            'INSERT INTO "user" (id, balance) VALUES (1, 123.45), (2, 0)'
        )
        connection.exec_driver_sql(
            "INSERT INTO ledger_entry VALUES (1, 1, 123.45, 123.45, 'opening_balance')"
        )
        with Operations.context(MigrationContext.configure(connection)):
            _revision().upgrade()
        yield connection
    engine.dispose()


def test_schema_upgrade_preserves_wallet_history_and_keeps_flags_off(
    migrated_connection,
):
    connection = migrated_connection
    assert connection.exec_driver_sql(
        'SELECT id, balance, unlimited_balance, exclude_from_business_metrics FROM "user" ORDER BY id'
    ).all() == [(1, 123.45, 0, 0), (2, 0, 0, 0)]
    assert connection.exec_driver_sql("SELECT * FROM ledger_entry").all() == [
        (1, 1, 123.45, 123.45, "opening_balance")
    ]
    assert (
        connection.exec_driver_sql("SELECT count(*) FROM subscription").scalar_one()
        == 0
    )
    # A raw insert proves database defaults too, independently of ORM defaults.
    connection.exec_driver_sql('INSERT INTO "user" (id, balance) VALUES (3, 10)')
    assert connection.exec_driver_sql(
        'SELECT unlimited_balance, exclude_from_business_metrics FROM "user" WHERE id = 3'
    ).one() == (0, 0)


def test_migration_matches_exported_model_metadata(migrated_connection):
    import core.db.models  # noqa: F401 - registers all mapped tables

    context = MigrationContext.configure(
        migrated_connection,
        opts={
            "include_object": lambda obj, name, type_, reflected, compare_to: (
                name in NEW_TABLES if type_ == "table" else True
            )
        },
    )
    assert not compare_metadata(context, Base.metadata)


@pytest.mark.parametrize(
    "values",
    [
        "'invalid', 'pending', 1",
        "'basic', 'invalid', 1",
        "'basic', 'pending', 0",
    ],
)
def test_invalid_entitlement_state_is_rejected(migrated_connection, values):
    with pytest.raises(IntegrityError), migrated_connection.begin_nested():
        migrated_connection.exec_driver_sql(
            "INSERT INTO subscription (id, user_id, plan_code, status, version) "
            f"VALUES ('invalid-sub', 1, {values})"
        )


def test_account_and_access_uniqueness_and_identity_retention(migrated_connection):
    connection = migrated_connection
    connection.exec_driver_sql(
        "INSERT INTO subscription (id, user_id, plan_code) VALUES "
        "('sub-1', 1, 'basic'), ('sub-2', 2, 'plus')"
    )
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.exec_driver_sql(
            "INSERT INTO subscription (id, user_id, plan_code) VALUES ('duplicate', 1, 'plus')"
        )
    connection.exec_driver_sql("INSERT INTO vpn_config VALUES (10)")
    connection.exec_driver_sql(
        "INSERT INTO subscription_access (subscription_id, service_id, config_id) "
        "VALUES ('sub-1', 'hysteria', 10)"
    )
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.exec_driver_sql(
            "INSERT INTO subscription_access (subscription_id, service_id, config_id) "
            "VALUES ('sub-2', 'hysteria', 10)"
        )
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.exec_driver_sql(
            "INSERT INTO subscription_access (subscription_id, service_id) "
            "VALUES ('sub-1', 'hysteria')"
        )
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.exec_driver_sql('DELETE FROM "user" WHERE id = 1')
    connection.exec_driver_sql("DELETE FROM vpn_config WHERE id = 10")
    assert connection.exec_driver_sql(
        "SELECT subscription_id, config_id, generation FROM subscription_access"
    ).one() == ("sub-1", None, 1)


@pytest.mark.parametrize(
    "history",
    ["subscription", "campaign", "unlimited_balance", "exclude_from_business_metrics"],
)
def test_downgrade_refuses_to_erase_business_or_manual_privilege_state(
    migrated_connection, history
):
    connection = migrated_connection
    if history == "subscription":
        connection.exec_driver_sql(
            "INSERT INTO subscription (id, user_id, plan_code) VALUES ('sub-1', 1, 'basic')"
        )
    elif history == "campaign":
        connection.exec_driver_sql(
            "INSERT INTO admin_campaign (public_id, name, text, audience, status, revision, preview_ids) "
            "VALUES ('campaign-1', 'Service status', 'Prepared only', '{}', 'draft', 1, '[]')"
        )
    else:
        connection.exec_driver_sql(f'UPDATE "user" SET {history} = true WHERE id = 1')
    with (
        Operations.context(MigrationContext.configure(connection)),
        pytest.raises(RuntimeError, match="Cannot downgrade"),
    ):
        _revision().downgrade()
    assert NEW_TABLES <= set(sa.inspect(connection).get_table_names())
    assert (
        connection.exec_driver_sql("SELECT count(*) FROM ledger_entry").scalar_one()
        == 1
    )


def test_unused_schema_can_be_downgraded_without_changing_wallet(migrated_connection):
    connection = migrated_connection
    with Operations.context(MigrationContext.configure(connection)):
        _revision().downgrade()
    assert not NEW_TABLES & set(sa.inspect(connection).get_table_names())
    assert (
        connection.exec_driver_sql(
            'SELECT balance FROM "user" WHERE id = 1'
        ).scalar_one()
        == 123.45
    )
    assert (
        connection.exec_driver_sql("SELECT count(*) FROM ledger_entry").scalar_one()
        == 1
    )


def test_postgresql_upgrade_compiles_without_business_data_changes():
    output = io.StringIO()
    context = MigrationContext.configure(
        url="postgresql://",
        opts={"as_sql": True, "literal_binds": True, "output_buffer": output},
    )
    with Operations.context(context):
        _revision().upgrade()
    sql = output.getvalue()
    assert "unlimited_balance BOOLEAN DEFAULT false NOT NULL" in sql
    assert "CREATE TABLE subscription" in sql
    assert "CREATE TABLE campaign_operation" in sql
    assert not any(word in sql for word in ("INSERT INTO", "UPDATE ", "DELETE FROM"))
