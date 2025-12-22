"""Add billing rates, ledger, and config billing state

Revision ID: 9f1c2d3e4a5b
Revises: d78ffcb95ce5
Create Date: 2025-06-30 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9f1c2d3e4a5b"
down_revision: Union[str, None] = "d78ffcb95ce5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "billing_settings",
        sa.Column("config_creation_cost", sa.Numeric(10, 2), nullable=False),
        sa.Column("monthly_config_cost", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "INSERT INTO billing_settings (id, config_creation_cost, monthly_config_cost) "
        "VALUES (1, 10.0, 50.0)"
    )

    op.create_table(
        "balance_transaction",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("config_id", sa.Integer(), nullable=True),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=256), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["config_id"], ["vpn_config.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_balance_transaction_user_id"),
        "balance_transaction",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_balance_transaction_created_at"),
        "balance_transaction",
        ["created_at"],
        unique=False,
    )

    op.add_column(
        "vpn_config",
        sa.Column(
            "last_billed_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
    )
    op.create_index(
        op.f("ix_vpn_config_last_billed_at"),
        "vpn_config",
        ["last_billed_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_vpn_config_last_billed_at"), table_name="vpn_config")
    op.drop_column("vpn_config", "last_billed_at")

    op.drop_index(op.f("ix_balance_transaction_created_at"), table_name="balance_transaction")
    op.drop_index(op.f("ix_balance_transaction_user_id"), table_name="balance_transaction")
    op.drop_table("balance_transaction")

    op.drop_table("billing_settings")
