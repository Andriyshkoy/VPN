"""add durable Telegram update inbox

Revision ID: c3a6f1e8b902
Revises: 4a9f0d6c2e31
Create Date: 2026-07-12 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "c3a6f1e8b902"
down_revision: Union[str, None] = "4a9f0d6c2e31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "telegram_update_inbox",
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("ordering_key", sa.String(length=80), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("lease_token", sa.String(length=36), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
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
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "update_id >= 0",
            name="ck_telegram_update_inbox_nonnegative_update_id",
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name="ck_telegram_update_inbox_nonnegative_attempts",
        ),
        sa.CheckConstraint(
            "source IN ('polling', 'webhook')",
            name="ck_telegram_update_inbox_source",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'failed', 'processed', 'dead')",
            name="ck_telegram_update_inbox_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_telegram_update_inbox_update_id",
        "telegram_update_inbox",
        ["update_id"],
        unique=True,
    )
    op.create_index(
        "ix_telegram_update_inbox_status",
        "telegram_update_inbox",
        ["status"],
    )
    op.create_index(
        "ix_telegram_update_inbox_ordering_key",
        "telegram_update_inbox",
        ["ordering_key"],
    )
    op.create_index(
        "ix_telegram_update_inbox_next_attempt_at",
        "telegram_update_inbox",
        ["next_attempt_at"],
    )
    op.create_index(
        "ix_telegram_update_inbox_lease_until",
        "telegram_update_inbox",
        ["lease_until"],
    )
    op.create_index(
        "ix_telegram_update_inbox_processed_at",
        "telegram_update_inbox",
        ["processed_at"],
    )
    op.create_index(
        "ix_telegram_update_inbox_status_next_attempt_at",
        "telegram_update_inbox",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_telegram_update_inbox_status_updated_at",
        "telegram_update_inbox",
        ["status", "updated_at"],
    )
    op.create_index(
        "ix_telegram_update_inbox_ordering_status_update",
        "telegram_update_inbox",
        ["ordering_key", "status", "update_id"],
    )


def downgrade() -> None:
    pending = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM telegram_update_inbox " "WHERE status <> 'processed'"
        )
    )
    if pending:
        raise RuntimeError(
            "Unsafe Telegram inbox downgrade refused while updates are unprocessed"
        )

    op.drop_index(
        "ix_telegram_update_inbox_ordering_status_update",
        table_name="telegram_update_inbox",
    )
    op.drop_index(
        "ix_telegram_update_inbox_status_updated_at",
        table_name="telegram_update_inbox",
    )
    op.drop_index(
        "ix_telegram_update_inbox_status_next_attempt_at",
        table_name="telegram_update_inbox",
    )
    op.drop_index(
        "ix_telegram_update_inbox_lease_until",
        table_name="telegram_update_inbox",
    )
    op.drop_index(
        "ix_telegram_update_inbox_processed_at",
        table_name="telegram_update_inbox",
    )
    op.drop_index(
        "ix_telegram_update_inbox_next_attempt_at",
        table_name="telegram_update_inbox",
    )
    op.drop_index(
        "ix_telegram_update_inbox_status",
        table_name="telegram_update_inbox",
    )
    op.drop_index(
        "ix_telegram_update_inbox_ordering_key",
        table_name="telegram_update_inbox",
    )
    op.drop_index(
        "ix_telegram_update_inbox_update_id",
        table_name="telegram_update_inbox",
    )
    op.drop_table("telegram_update_inbox")
