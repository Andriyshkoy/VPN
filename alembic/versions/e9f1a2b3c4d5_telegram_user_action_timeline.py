"""add immutable Telegram user actions for the admin timeline

Revision ID: e9f1a2b3c4d5
Revises: d4e7f9a1b2c3
Create Date: 2026-07-14 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "e9f1a2b3c4d5"
down_revision: Union[str, None] = "d4e7f9a1b2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _install_immutability_guard() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION vpn_reject_telegram_user_action_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'telegram_user_action_event rows are immutable';
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_telegram_user_action_immutable
            BEFORE UPDATE OR DELETE ON telegram_user_action_event
            FOR EACH ROW EXECUTE FUNCTION vpn_reject_telegram_user_action_mutation()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_telegram_user_action_no_truncate
            BEFORE TRUNCATE ON telegram_user_action_event
            FOR EACH STATEMENT EXECUTE FUNCTION vpn_reject_telegram_user_action_mutation()
            """
        )
    )


def upgrade() -> None:
    op.create_table(
        "telegram_user_action_event",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("source_update_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "category",
            sa.String(length=16),
            nullable=False,
            server_default="bot",
        ),
        sa.Column("action", sa.String(length=96), nullable=False),
        sa.Column("result", sa.String(length=24), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "source_update_id >= 0",
            name="ck_telegram_user_action_nonnegative_update_id",
        ),
        sa.CheckConstraint(
            "category = 'bot'",
            name="ck_telegram_user_action_category",
        ),
        sa.CheckConstraint(
            "result IN ('handled', 'completed', 'rejected', 'ignored', "
            "'invalid', 'unavailable', 'failed')",
            name="ck_telegram_user_action_result",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_update_id",
            name="uq_telegram_user_action_source_update_id",
        ),
    )
    op.create_index(
        "ix_telegram_user_action_event_user_id",
        "telegram_user_action_event",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_telegram_user_action_event_source_update_id",
        "telegram_user_action_event",
        ["source_update_id"],
        unique=False,
    )
    op.create_index(
        "ix_telegram_user_action_event_action",
        "telegram_user_action_event",
        ["action"],
        unique=False,
    )
    op.create_index(
        "ix_telegram_user_action_event_result",
        "telegram_user_action_event",
        ["result"],
        unique=False,
    )
    op.create_index(
        "ix_telegram_user_action_event_occurred_at",
        "telegram_user_action_event",
        ["occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_telegram_user_action_user_occurred",
        "telegram_user_action_event",
        ["user_id", "occurred_at", "id"],
        unique=False,
    )
    _install_immutability_guard()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_telegram_user_action_no_truncate "
            "ON telegram_user_action_event"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_telegram_user_action_immutable "
            "ON telegram_user_action_event"
        )
        op.execute("DROP FUNCTION IF EXISTS vpn_reject_telegram_user_action_mutation()")
    op.drop_index(
        "ix_telegram_user_action_user_occurred",
        table_name="telegram_user_action_event",
    )
    op.drop_index(
        "ix_telegram_user_action_event_occurred_at",
        table_name="telegram_user_action_event",
    )
    op.drop_index(
        "ix_telegram_user_action_event_result",
        table_name="telegram_user_action_event",
    )
    op.drop_index(
        "ix_telegram_user_action_event_action",
        table_name="telegram_user_action_event",
    )
    op.drop_index(
        "ix_telegram_user_action_event_source_update_id",
        table_name="telegram_user_action_event",
    )
    op.drop_index(
        "ix_telegram_user_action_event_user_id",
        table_name="telegram_user_action_event",
    )
    op.drop_table("telegram_user_action_event")
