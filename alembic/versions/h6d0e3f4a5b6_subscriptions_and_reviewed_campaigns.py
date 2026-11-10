"""Add account subscriptions and reviewed administrative campaigns.

Revision ID: h6d0e3f4a5b6
Revises: g5c9d2e3f4a5
Create Date: 2026-11-09

Schema only: wallet/ledger rows, existing VPN access and billing switches are
unchanged. Legacy plan assignment is a separate explicitly reviewed operation.
No account receives unlimited balance or analytics exclusion automatically.
"""

import sqlalchemy as sa

from alembic import op

revision = "h6d0e3f4a5b6"
down_revision = "g5c9d2e3f4a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column(
            "unlimited_balance", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "user",
        sa.Column(
            "exclude_from_business_metrics",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_table(
        "subscription",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("plan_code", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("migration_source", sa.String(64), nullable=True),
        sa.Column(
            "migration_snapshot",
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("user_id", name="uq_subscription_user_id"),
        sa.CheckConstraint(
            "plan_code IN ('basic', 'plus')", name="ck_subscription_plan"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'suspended', 'expired', 'cancelled')",
            name="ck_subscription_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_subscription_positive_version"),
    )
    op.create_table(
        "subscription_access",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "subscription_id",
            sa.String(36),
            sa.ForeignKey("subscription.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("service_id", sa.String(32), nullable=False),
        sa.Column(
            "config_id",
            sa.Integer(),
            sa.ForeignKey("vpn_config.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("provision_operation_id", sa.String(36), nullable=True),
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
        sa.UniqueConstraint(
            "subscription_id", "service_id", name="uq_subscription_access_service"
        ),
        sa.UniqueConstraint("config_id", name="uq_subscription_access_config_id"),
        sa.CheckConstraint(
            "service_id IN ('hysteria', 'vless-reality')",
            name="ck_subscription_access_service",
        ),
        sa.CheckConstraint("generation >= 1", name="ck_subscription_access_generation"),
    )
    op.create_index(
        "ix_subscription_access_subscription_id",
        "subscription_access",
        ["subscription_id"],
    )
    op.create_table(
        "admin_campaign",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("audience", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "created_by_admin_id",
            sa.Integer(),
            sa.ForeignKey("admin_user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("preview_token_hash", sa.String(64), nullable=True),
        sa.Column("preview_digest", sa.String(64), nullable=True),
        sa.Column("preview_ids", sa.JSON(), nullable=False),
        sa.Column("previewed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column("launched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("public_id", name="uq_admin_campaign_public_id"),
        sa.CheckConstraint(
            "status IN ('draft','queued','running','completed','cancelled')",
            name="ck_admin_campaign_status",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_admin_campaign_revision"),
    )
    op.create_index("ix_admin_campaign_public_id", "admin_campaign", ["public_id"])
    op.create_index("ix_admin_campaign_status", "admin_campaign", ["status"])
    op.create_table(
        "campaign_recipient",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey("admin_campaign.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("tg_id_snapshot", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("outbox_dedupe_key", sa.String(160), nullable=False),
        sa.Column(
            "outbox_id",
            sa.Integer(),
            sa.ForeignKey("notification_outbox.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(96), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "campaign_id", "user_id", name="uq_campaign_recipient_campaign_user"
        ),
        sa.UniqueConstraint("outbox_dedupe_key", name="uq_campaign_recipient_dedupe"),
        sa.CheckConstraint(
            "status IN ('pending','queued','sending','delivered','failed','unknown','skipped','cancelled')",
            name="ck_campaign_recipient_status",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND generation >= 0", name="ck_campaign_recipient_attempts"
        ),
    )
    op.create_index(
        "ix_campaign_recipient_campaign_id", "campaign_recipient", ["campaign_id"]
    )
    op.create_index("ix_campaign_recipient_user_id", "campaign_recipient", ["user_id"])
    op.create_index("ix_campaign_recipient_status", "campaign_recipient", ["status"])
    op.create_index(
        "ix_campaign_recipient_campaign_status_id",
        "campaign_recipient",
        ["campaign_id", "status", "id"],
    )
    op.create_table(
        "campaign_operation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey("admin_campaign.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("recipient_ids", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "campaign_id", "kind", "key_hash", name="uq_campaign_operation_idempotency"
        ),
        sa.CheckConstraint(
            "kind IN ('launch','retry')", name="ck_campaign_operation_kind"
        ),
    )
    op.create_index(
        "ix_campaign_operation_campaign_id", "campaign_operation", ["campaign_id"]
    )


def downgrade() -> None:
    # Refuse to erase account entitlement or campaign delivery/idempotency history.
    bind = op.get_bind()
    for table in (
        "subscription_access",
        "subscription",
        "campaign_recipient",
        "campaign_operation",
        "admin_campaign",
    ):
        if bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one():
            raise RuntimeError(
                f"Cannot downgrade while {table} business history exists"
            )
    if bind.execute(
        sa.text(
            'SELECT count(*) FROM "user" WHERE unlimited_balance OR exclude_from_business_metrics'
        )
    ).scalar_one():
        raise RuntimeError(
            "Cannot downgrade while manual user privilege or metrics flags are enabled"
        )
    op.drop_index("ix_campaign_operation_campaign_id", table_name="campaign_operation")
    op.drop_table("campaign_operation")
    op.drop_index(
        "ix_campaign_recipient_campaign_status_id", table_name="campaign_recipient"
    )
    op.drop_index("ix_campaign_recipient_status", table_name="campaign_recipient")
    op.drop_index("ix_campaign_recipient_user_id", table_name="campaign_recipient")
    op.drop_index("ix_campaign_recipient_campaign_id", table_name="campaign_recipient")
    op.drop_table("campaign_recipient")
    op.drop_index("ix_admin_campaign_status", table_name="admin_campaign")
    op.drop_index("ix_admin_campaign_public_id", table_name="admin_campaign")
    op.drop_table("admin_campaign")
    op.drop_index(
        "ix_subscription_access_subscription_id", table_name="subscription_access"
    )
    op.drop_table("subscription_access")
    op.drop_table("subscription")
    op.drop_column("user", "exclude_from_business_metrics")
    op.drop_column("user", "unlimited_balance")
