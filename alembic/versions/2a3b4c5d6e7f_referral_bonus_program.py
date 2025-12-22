"""Add referral bonus settings and tracking

Revision ID: 2a3b4c5d6e7f
Revises: 9f1c2d3e4a5b
Create Date: 2025-06-30 12:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "2a3b4c5d6e7f"
down_revision: Union[str, None] = "9f1c2d3e4a5b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "billing_settings",
        sa.Column(
            "referral_first_deposit_bonus_pct", sa.Numeric(5, 2), nullable=False, server_default="50"
        ),
    )
    op.add_column(
        "billing_settings",
        sa.Column(
            "referral_recurring_bonus_pct", sa.Numeric(5, 2), nullable=False, server_default="10"
        ),
    )
    op.execute(
        "UPDATE billing_settings SET referral_first_deposit_bonus_pct = 50, "
        "referral_recurring_bonus_pct = 10 WHERE id = 1"
    )

    op.add_column(
        "user",
        sa.Column("referral_first_bonus_paid", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.add_column(
        "balance_transaction",
        sa.Column("related_user_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        op.f("ix_balance_transaction_related_user_id"),
        "balance_transaction",
        ["related_user_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_balance_transaction_related_user_id",
        "balance_transaction",
        "user",
        ["related_user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        "fk_balance_transaction_related_user_id",
        "balance_transaction",
        type_="foreignkey",
    )
    op.drop_index(
        op.f("ix_balance_transaction_related_user_id"),
        table_name="balance_transaction",
    )
    op.drop_column("balance_transaction", "related_user_id")

    op.drop_column("user", "referral_first_bonus_paid")

    op.drop_column("billing_settings", "referral_recurring_bonus_pct")
    op.drop_column("billing_settings", "referral_first_deposit_bonus_pct")
