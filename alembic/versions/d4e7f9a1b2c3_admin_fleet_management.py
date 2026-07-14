"""add managed VPN fleet lifecycle, status history, and durable admin actions

Revision ID: d4e7f9a1b2c3
Revises: a6b4c2d8e901
Create Date: 2026-07-14 00:00:01.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "d4e7f9a1b2c3"
down_revision: Union[str, None] = "a6b4c2d8e901"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("server") as batch:
        batch.add_column(
            sa.Column(
                "lifecycle_state",
                sa.String(length=16),
                nullable=False,
                server_default="active",
            )
        )
        batch.add_column(
            sa.Column(
                "accepts_new_configs",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch.add_column(sa.Column("max_configs", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column(
                "capacity_reserve",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column(
                "placement_weight",
                sa.Numeric(precision=8, scale=3),
                nullable=False,
                server_default="1",
            )
        )
        batch.add_column(sa.Column("provider", sa.String(length=128), nullable=True))
        batch.add_column(
            sa.Column("public_endpoint", sa.String(length=255), nullable=True)
        )
        batch.add_column(
            sa.Column("manager_instance_id", sa.String(length=36), nullable=True)
        )
        batch.add_column(
            sa.Column("version", sa.Integer(), nullable=False, server_default="1")
        )
        batch.add_column(
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            )
        )
        batch.create_check_constraint(
            "ck_server_lifecycle_state",
            "lifecycle_state IN ('active', 'draining', 'disabled', 'retired')",
        )
        batch.create_check_constraint(
            "ck_server_positive_max_configs",
            "max_configs IS NULL OR max_configs > 0",
        )
        batch.create_check_constraint(
            "ck_server_nonnegative_capacity_reserve", "capacity_reserve >= 0"
        )
        batch.create_check_constraint(
            "ck_server_reserve_below_capacity",
            "max_configs IS NULL OR capacity_reserve < max_configs",
        )
        batch.create_check_constraint(
            "ck_server_positive_placement_weight", "placement_weight > 0"
        )
        batch.create_check_constraint("ck_server_positive_version", "version >= 1")
        batch.create_index(
            "ix_server_lifecycle_state", ["lifecycle_state"], unique=False
        )
        batch.create_index(
            "ix_server_accepts_new_configs", ["accepts_new_configs"], unique=False
        )
        batch.create_index(
            "ix_server_manager_instance_id", ["manager_instance_id"], unique=False
        )

    op.create_table(
        "vpn_server_status",
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=96), nullable=True),
        sa.Column("manager_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("manager_version", sa.String(length=64), nullable=True),
        sa.Column("manager_instance_id", sa.String(length=36), nullable=True),
        sa.Column("inventory_revision", sa.String(length=160), nullable=True),
        sa.Column(
            "collected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('status', 'inventory')", name="ck_vpn_server_status_kind"
        ),
        sa.ForeignKeyConstraint(["server_id"], ["server.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_vpn_server_status_server_id",
        "vpn_server_status",
        ["server_id"],
        unique=False,
    )
    op.create_index(
        "ix_vpn_server_status_manager_instance_id",
        "vpn_server_status",
        ["manager_instance_id"],
        unique=False,
    )
    op.create_index(
        "ix_vpn_server_status_inventory_revision",
        "vpn_server_status",
        ["inventory_revision"],
        unique=False,
    )
    op.create_index(
        "ix_vpn_server_status_collected_at",
        "vpn_server_status",
        ["collected_at"],
        unique=False,
    )
    op.create_index(
        "ix_vpn_server_status_server_collected",
        "vpn_server_status",
        ["server_id", "collected_at"],
        unique=False,
    )

    op.create_table(
        "admin_action",
        sa.Column("action_id", sa.String(length=36), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="pending"
        ),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("expected_server_version", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=96), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_admin_action_status",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["admin_user.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["server_id"], ["server.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_user_id",
            "idempotency_key_hash",
            name="uq_admin_action_actor_idempotency",
        ),
    )
    op.create_index(
        "ix_admin_action_action_id", "admin_action", ["action_id"], unique=True
    )
    op.create_index(
        "ix_admin_action_server_id", "admin_action", ["server_id"], unique=False
    )
    op.create_index(
        "ix_admin_action_actor_user_id",
        "admin_action",
        ["actor_user_id"],
        unique=False,
    )
    op.create_index("ix_admin_action_kind", "admin_action", ["kind"], unique=False)
    op.create_index("ix_admin_action_status", "admin_action", ["status"], unique=False)
    op.create_index(
        "ix_admin_action_created_at", "admin_action", ["created_at"], unique=False
    )
    op.create_index(
        "ix_admin_action_server_created",
        "admin_action",
        ["server_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_admin_action_server_created", table_name="admin_action")
    op.drop_index("ix_admin_action_created_at", table_name="admin_action")
    op.drop_index("ix_admin_action_status", table_name="admin_action")
    op.drop_index("ix_admin_action_kind", table_name="admin_action")
    op.drop_index("ix_admin_action_actor_user_id", table_name="admin_action")
    op.drop_index("ix_admin_action_server_id", table_name="admin_action")
    op.drop_index("ix_admin_action_action_id", table_name="admin_action")
    op.drop_table("admin_action")

    op.drop_index(
        "ix_vpn_server_status_server_collected", table_name="vpn_server_status"
    )
    op.drop_index("ix_vpn_server_status_collected_at", table_name="vpn_server_status")
    op.drop_index(
        "ix_vpn_server_status_inventory_revision", table_name="vpn_server_status"
    )
    op.drop_index(
        "ix_vpn_server_status_manager_instance_id", table_name="vpn_server_status"
    )
    op.drop_index("ix_vpn_server_status_server_id", table_name="vpn_server_status")
    op.drop_table("vpn_server_status")

    with op.batch_alter_table("server") as batch:
        batch.drop_index("ix_server_manager_instance_id")
        batch.drop_index("ix_server_accepts_new_configs")
        batch.drop_index("ix_server_lifecycle_state")
        batch.drop_constraint("ck_server_positive_version", type_="check")
        batch.drop_constraint("ck_server_positive_placement_weight", type_="check")
        batch.drop_constraint("ck_server_reserve_below_capacity", type_="check")
        batch.drop_constraint("ck_server_nonnegative_capacity_reserve", type_="check")
        batch.drop_constraint("ck_server_positive_max_configs", type_="check")
        batch.drop_constraint("ck_server_lifecycle_state", type_="check")
        batch.drop_column("updated_at")
        batch.drop_column("version")
        batch.drop_column("manager_instance_id")
        batch.drop_column("public_endpoint")
        batch.drop_column("provider")
        batch.drop_column("placement_weight")
        batch.drop_column("capacity_reserve")
        batch.drop_column("max_configs")
        batch.drop_column("accepts_new_configs")
        batch.drop_column("lifecycle_state")
