"""change balance to Numeric

Revision ID: 7cdd648f17a9
Revises: b73ad1f7657e
Create Date: 2025-06-10 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "7cdd648f17a9"
down_revision: Union[str, None] = "b73ad1f7657e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "user", "balance", type_=sa.Numeric(10, 2), existing_type=sa.Float()
    )


def downgrade() -> None:
    op.alter_column(
        "user", "balance", type_=sa.Float(), existing_type=sa.Numeric(10, 2)
    )
