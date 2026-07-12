"""add financial ledger and recoverable VPN lifecycle

Revision ID: 4a9f0d6c2e31
Revises: d78ffcb95ce5
Create Date: 2026-07-12 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "4a9f0d6c2e31"
down_revision: Union[str, None] = "d78ffcb95ce5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _normalize_referral_constraint() -> None:
    """Give the legacy unnamed FK a stable name so rollback is reliable."""

    bind = op.get_bind()
    for constraint in sa.inspect(bind).get_foreign_keys("user"):
        if constraint.get("constrained_columns") != ["referred_by_id"]:
            continue
        name = constraint.get("name")
        if name == "fk_user_referred_by_id_user":
            return
        if name:
            op.drop_constraint(name, "user", type_="foreignkey")
        op.create_foreign_key(
            "fk_user_referred_by_id_user",
            "user",
            "user",
            ["referred_by_id"],
            ["id"],
        )
        return


def _replace_vpn_config_server_fk(*, ondelete: str) -> None:
    """Replace the legacy cascading server FK with an explicit policy."""

    bind = op.get_bind()
    for constraint in sa.inspect(bind).get_foreign_keys("vpn_config"):
        if (
            constraint.get("constrained_columns") != ["server_id"]
            or constraint.get("referred_table") != "server"
        ):
            continue
        name = constraint.get("name")
        if not name:
            raise RuntimeError("vpn_config.server_id foreign key must be named")
        op.drop_constraint(name, "vpn_config", type_="foreignkey")
        break
    op.create_foreign_key(
        "fk_vpn_config_server_id_server",
        "vpn_config",
        "server",
        ["server_id"],
        ["id"],
        ondelete=ondelete,
    )


def _install_ledger_immutability_guard() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION vpn_reject_ledger_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'ledger_entry rows are immutable';
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_ledger_entry_immutable
            BEFORE UPDATE OR DELETE ON ledger_entry
            FOR EACH ROW EXECUTE FUNCTION vpn_reject_ledger_mutation()
            """
        )
    )


def _drop_ledger_immutability_guard() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        sa.text("DROP TRIGGER IF EXISTS trg_ledger_entry_immutable ON ledger_entry")
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS vpn_reject_ledger_mutation()"))


def _assert_safe_downgrade() -> None:
    """Refuse a schema rollback that would silently erase production facts."""

    bind = op.get_bind()
    financial_events = bind.scalar(
        sa.text("SELECT count(*) FROM ledger_entry WHERE kind <> 'opening_balance'")
    )
    operational_events = sum(
        int(bind.scalar(sa.text(f"SELECT count(*) FROM {table}")) or 0)
        for table in (
            "provider_payment",
            "billing_run",
            "notification_outbox",
            "vpn_operation",
        )
    )
    oversized_balance = bind.scalar(
        sa.text(
            'SELECT count(*) FROM "user" '
            "WHERE balance > 99999999.99 OR balance < -99999999.99"
        )
    )
    if financial_events or operational_events or oversized_balance:
        raise RuntimeError(
            "Unsafe financial/lifecycle downgrade refused; use a code-only rollback"
        )


def upgrade() -> None:
    _normalize_referral_constraint()
    _replace_vpn_config_server_fk(ondelete="RESTRICT")

    op.alter_column(
        "user",
        "balance",
        existing_type=sa.Numeric(10, 2),
        type_=sa.Numeric(18, 2),
        existing_nullable=False,
    )
    op.add_column(
        "user",
        sa.Column(
            "telegram_delivery_status",
            sa.String(length=32),
            nullable=False,
            server_default="active",
        ),
    )
    op.add_column(
        "user",
        sa.Column("telegram_blocked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "user",
        sa.Column("telegram_last_delivery_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "user",
        sa.Column(
            "telegram_delivery_status_updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_user_telegram_delivery_status",
        "user",
        ["telegram_delivery_status"],
    )

    op.add_column(
        "vpn_config",
        sa.Column(
            "desired_state",
            sa.String(length=32),
            nullable=False,
            server_default="active",
        ),
    )
    op.add_column(
        "vpn_config",
        sa.Column(
            "actual_state",
            sa.String(length=32),
            nullable=False,
            server_default="active",
        ),
    )
    op.add_column(
        "vpn_config", sa.Column("operation_id", sa.String(length=36), nullable=True)
    )
    op.add_column("vpn_config", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column(
        "vpn_config",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE vpn_config
            SET desired_state = CASE WHEN suspended THEN 'suspended' ELSE 'active' END,
                actual_state = CASE WHEN suspended THEN 'suspended' ELSE 'active' END
            """
        )
    )
    op.create_index(
        "ix_vpn_config_desired_state", "vpn_config", ["desired_state"], unique=False
    )
    op.create_index(
        "ix_vpn_config_actual_state", "vpn_config", ["actual_state"], unique=False
    )
    op.create_index(
        "ix_vpn_config_operation_id", "vpn_config", ["operation_id"], unique=False
    )

    op.create_table(
        "ledger_entry",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("balance_after", sa.Numeric(18, 2), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("reference_type", sa.String(length=32), nullable=True),
        sa.Column("reference_id", sa.String(length=160), nullable=True),
        sa.Column(
            "details",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.CheckConstraint("amount <> 0", name="ck_ledger_entry_nonzero_amount"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_ledger_entry_idempotency_key"),
    )
    op.create_index("ix_ledger_entry_user_id", "ledger_entry", ["user_id"])
    op.create_index("ix_ledger_entry_kind", "ledger_entry", ["kind"])
    op.create_index("ix_ledger_entry_created_at", "ledger_entry", ["created_at"])

    user = sa.table(
        "user",
        sa.column("id", sa.Integer()),
        sa.column("balance", sa.Numeric(18, 2)),
    )
    ledger = sa.table(
        "ledger_entry",
        sa.column("user_id", sa.Integer()),
        sa.column("amount", sa.Numeric(18, 2)),
        sa.column("balance_after", sa.Numeric(18, 2)),
        sa.column("kind", sa.String()),
        sa.column("idempotency_key", sa.String()),
    )
    opening_key = sa.literal("opening-balance:user:") + sa.cast(user.c.id, sa.String())
    op.get_bind().execute(
        ledger.insert().from_select(
            ["user_id", "amount", "balance_after", "kind", "idempotency_key"],
            sa.select(
                user.c.id,
                user.c.balance,
                user.c.balance,
                sa.literal("opening_balance"),
                opening_key,
            ).where(user.c.balance != 0),
        )
    )
    _install_ledger_immutability_guard()

    op.create_table(
        "billing_run",
        sa.Column("period_key", sa.String(length=80), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cost_per_config", sa.Numeric(18, 2), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("charged_users", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "total_amount", sa.Numeric(18, 2), nullable=False, server_default="0"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "period_end > period_start", name="ck_billing_run_valid_period"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_billing_run_period_key", "billing_run", ["period_key"], unique=True
    )
    op.create_index("ix_billing_run_status", "billing_run", ["status"])

    op.create_table(
        "notification_outbox",
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.String(length=24), nullable=False, server_default="pending"
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_notification_outbox_dedupe_key",
        "notification_outbox",
        ["dedupe_key"],
        unique=True,
    )
    op.create_index(
        "ix_notification_outbox_chat_id", "notification_outbox", ["chat_id"]
    )
    op.create_index("ix_notification_outbox_status", "notification_outbox", ["status"])
    op.create_index(
        "ix_notification_outbox_next_attempt_at",
        "notification_outbox",
        ["next_attempt_at"],
    )

    op.create_table(
        "provider_payment",
        sa.Column("intent_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_payment_id", sa.String(length=160), nullable=True),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("payload", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("ledger_entry_id", sa.Integer(), nullable=True),
        sa.Column(
            "raw_data", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP + INTERVAL '1 hour'"),
        ),
        sa.Column("credited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_provider_payment_positive_amount"),
        sa.ForeignKeyConstraint(
            ["ledger_entry_id"], ["ledger_entry.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ledger_entry_id"),
        sa.UniqueConstraint(
            "provider",
            "provider_payment_id",
            name="uq_provider_payment_provider_charge",
        ),
    )
    op.create_index(
        "ix_provider_payment_intent_id",
        "provider_payment",
        ["intent_id"],
        unique=True,
    )
    op.create_index("ix_provider_payment_user_id", "provider_payment", ["user_id"])
    op.create_index("ix_provider_payment_payload", "provider_payment", ["payload"])
    op.create_index("ix_provider_payment_status", "provider_payment", ["status"])
    op.create_index(
        "ix_provider_payment_expires_at", "provider_payment", ["expires_at"]
    )

    op.create_table(
        "vpn_operation",
        sa.Column("operation_id", sa.String(length=36), nullable=False),
        sa.Column("config_id", sa.Integer(), nullable=True),
        sa.Column("config_name", sa.String(length=128), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=True),
        sa.Column("owner_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("lease_token", sa.String(length=36), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["config_id"], ["vpn_config.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["owner_id"], ["user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["server_id"], ["server.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_vpn_operation_operation_id",
        "vpn_operation",
        ["operation_id"],
        unique=True,
    )
    op.create_index("ix_vpn_operation_config_id", "vpn_operation", ["config_id"])
    op.create_index("ix_vpn_operation_config_name", "vpn_operation", ["config_name"])
    op.create_index("ix_vpn_operation_server_id", "vpn_operation", ["server_id"])
    op.create_index("ix_vpn_operation_owner_id", "vpn_operation", ["owner_id"])
    op.create_index("ix_vpn_operation_status", "vpn_operation", ["status"])
    op.create_index(
        "ix_vpn_operation_next_attempt_at",
        "vpn_operation",
        ["next_attempt_at"],
    )
    op.create_index("ix_vpn_operation_lease_until", "vpn_operation", ["lease_until"])
    op.create_index(
        "ix_vpn_operation_status_next_attempt_at",
        "vpn_operation",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    _assert_safe_downgrade()
    op.drop_index("ix_vpn_operation_status_next_attempt_at", table_name="vpn_operation")
    op.drop_index("ix_vpn_operation_lease_until", table_name="vpn_operation")
    op.drop_index("ix_vpn_operation_next_attempt_at", table_name="vpn_operation")
    op.drop_index("ix_vpn_operation_status", table_name="vpn_operation")
    op.drop_index("ix_vpn_operation_owner_id", table_name="vpn_operation")
    op.drop_index("ix_vpn_operation_server_id", table_name="vpn_operation")
    op.drop_index("ix_vpn_operation_config_name", table_name="vpn_operation")
    op.drop_index("ix_vpn_operation_config_id", table_name="vpn_operation")
    op.drop_index("ix_vpn_operation_operation_id", table_name="vpn_operation")
    op.drop_table("vpn_operation")

    op.drop_index("ix_provider_payment_expires_at", table_name="provider_payment")
    op.drop_index("ix_provider_payment_status", table_name="provider_payment")
    op.drop_index("ix_provider_payment_payload", table_name="provider_payment")
    op.drop_index("ix_provider_payment_user_id", table_name="provider_payment")
    op.drop_index("ix_provider_payment_intent_id", table_name="provider_payment")
    op.drop_table("provider_payment")

    op.drop_index(
        "ix_notification_outbox_next_attempt_at", table_name="notification_outbox"
    )
    op.drop_index("ix_notification_outbox_status", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_chat_id", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_dedupe_key", table_name="notification_outbox")
    op.drop_table("notification_outbox")

    op.drop_index("ix_billing_run_status", table_name="billing_run")
    op.drop_index("ix_billing_run_period_key", table_name="billing_run")
    op.drop_table("billing_run")

    op.drop_index("ix_ledger_entry_created_at", table_name="ledger_entry")
    op.drop_index("ix_ledger_entry_kind", table_name="ledger_entry")
    op.drop_index("ix_ledger_entry_user_id", table_name="ledger_entry")
    _drop_ledger_immutability_guard()
    op.drop_table("ledger_entry")

    op.drop_index("ix_vpn_config_operation_id", table_name="vpn_config")
    op.drop_index("ix_vpn_config_actual_state", table_name="vpn_config")
    op.drop_index("ix_vpn_config_desired_state", table_name="vpn_config")
    op.drop_column("vpn_config", "updated_at")
    op.drop_column("vpn_config", "last_error")
    op.drop_column("vpn_config", "operation_id")
    op.drop_column("vpn_config", "actual_state")
    op.drop_column("vpn_config", "desired_state")

    op.drop_index("ix_user_telegram_delivery_status", table_name="user")
    op.drop_column("user", "telegram_delivery_status_updated_at")
    op.drop_column("user", "telegram_last_delivery_error")
    op.drop_column("user", "telegram_blocked_at")
    op.drop_column("user", "telegram_delivery_status")

    op.alter_column(
        "user",
        "balance",
        existing_type=sa.Numeric(18, 2),
        type_=sa.Numeric(10, 2),
        existing_nullable=False,
    )
    _replace_vpn_config_server_fk(ondelete="CASCADE")
