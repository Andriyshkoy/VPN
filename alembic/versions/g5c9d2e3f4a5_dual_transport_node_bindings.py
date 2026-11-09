"""Add node-agent metadata and durable access bindings without migrating money.

Revision ID: g5c9d2e3f4a5
Revises: f4b8c1d2e3a4
Create Date: 2026-11-09
"""

import sqlalchemy as sa

from alembic import op

revision = "g5c9d2e3f4a5"
down_revision = "f4b8c1d2e3a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "server",
        sa.Column(
            "backend_type",
            sa.String(32),
            nullable=False,
            server_default="openvpn-manager",
        ),
    )
    op.add_column("server", sa.Column("node_id", sa.String(63), nullable=True))
    op.add_column(
        "server",
        sa.Column(
            "endpoint_revision",
            sa.Integer(),
            sa.CheckConstraint(
                "endpoint_revision >= 1", name="ck_server_positive_endpoint_revision"
            ),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column("vpn_config", sa.Column("service_id", sa.String(32), nullable=True))
    op.add_column(
        "vpn_config",
        sa.Column(
            "access_policy", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
        ),
    )
    op.create_table(
        "vpn_access_binding",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "config_id",
            sa.Integer(),
            sa.ForeignKey("vpn_config.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("config_ref", sa.Integer(), nullable=False),
        sa.Column(
            "server_id",
            sa.Integer(),
            sa.ForeignKey("server.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("node_id", sa.String(63), nullable=False),
        sa.Column("service_id", sa.String(32), nullable=False),
        sa.Column("provision_operation_id", sa.String(36), nullable=False),
        sa.Column("remote_access_id", sa.String(36), nullable=True),
        sa.Column("policy", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "observed_state",
            sa.String(32),
            nullable=False,
            server_default="provisioning",
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint("config_id", name="uq_vpn_binding_config_id"),
        sa.UniqueConstraint("config_ref", name="uq_vpn_binding_config_ref"),
        sa.UniqueConstraint(
            "provision_operation_id", name="uq_vpn_binding_provision_operation"
        ),
        sa.UniqueConstraint(
            "node_id", "remote_access_id", name="uq_vpn_binding_remote_access"
        ),
        sa.CheckConstraint(
            "service_id IN ('hysteria', 'vless-reality')",
            name="ck_vpn_binding_service_id",
        ),
    )
    op.create_index(
        "ix_vpn_access_binding_server_id", "vpn_access_binding", ["server_id"]
    )


def downgrade() -> None:
    # Access identities are recovery records. Explicitly refuse to erase them.
    count = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM vpn_access_binding"))
        .scalar_one()
    )
    if count:
        raise RuntimeError(
            "Cannot downgrade while durable node bindings exist; retain their recovery history"
        )
    op.drop_index("ix_vpn_access_binding_server_id", table_name="vpn_access_binding")
    op.drop_table("vpn_access_binding")
    op.drop_column("vpn_config", "access_policy")
    op.drop_column("vpn_config", "service_id")
    op.drop_column("server", "node_id")
    op.drop_column("server", "endpoint_revision")
    op.drop_column("server", "backend_type")
