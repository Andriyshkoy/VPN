"""store VPN suspension timestamps as timezone-aware UTC instants

Revision ID: f4b8c1d2e3a4
Revises: e9f1a2b3c4d5
Create Date: 2026-07-27 10:30:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "f4b8c1d2e3a4"
down_revision: Union[str, None] = "e9f1a2b3c4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.alter_column(
        "vpn_config",
        "suspended_at",
        existing_type=sa.DateTime(timezone=False),
        type_=sa.DateTime(timezone=True),
        existing_nullable=True,
        postgresql_using="suspended_at AT TIME ZONE 'UTC'",
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.alter_column(
        "vpn_config",
        "suspended_at",
        existing_type=sa.DateTime(timezone=True),
        type_=sa.DateTime(timezone=False),
        existing_nullable=True,
        postgresql_using="suspended_at AT TIME ZONE 'UTC'",
    )
