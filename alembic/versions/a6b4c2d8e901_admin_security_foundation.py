"""add database-backed admin sessions, roles, and immutable audit events

Revision ID: a6b4c2d8e901
Revises: f1a8c3d9e742
Create Date: 2026-07-14 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "a6b4c2d8e901"
down_revision: Union[str, None] = "f1a8c3d9e742"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _install_admin_audit_immutability_guard() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION vpn_reject_admin_audit_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'admin_audit_event rows are immutable';
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_admin_audit_event_immutable
            BEFORE UPDATE OR DELETE ON admin_audit_event
            FOR EACH ROW EXECUTE FUNCTION vpn_reject_admin_audit_mutation()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_admin_audit_event_no_truncate
            BEFORE TRUNCATE ON admin_audit_event
            FOR EACH STATEMENT EXECUTE FUNCTION vpn_reject_admin_audit_mutation()
            """
        )
    )


def upgrade() -> None:
    op.create_table(
        "admin_user",
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "failed_login_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "password_changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
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
        sa.Column("id", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "role IN ('owner', 'support', 'finance', 'ops', 'viewer')",
            name="ck_admin_user_role",
        ),
        sa.CheckConstraint(
            "failed_login_attempts >= 0",
            name="ck_admin_user_nonnegative_failed_logins",
        ),
        sa.CheckConstraint(
            "username = lower(username)",
            name="ck_admin_user_normalized_username",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="uq_admin_user_username"),
    )

    op.create_table(
        "admin_session",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("client_ip_hash", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["admin_user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_admin_session_token_hash"),
    )
    op.create_index(
        "ix_admin_session_user_id", "admin_session", ["user_id"], unique=False
    )
    op.create_index(
        "ix_admin_session_expires_at",
        "admin_session",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_admin_session_revoked_at",
        "admin_session",
        ["revoked_at"],
        unique=False,
    )

    op.create_table(
        "admin_audit_event",
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=96), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=True),
        sa.Column("target_id", sa.String(length=160), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("client_ip_hash", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["admin_user.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_admin_audit_event_actor_user_id",
        "admin_audit_event",
        ["actor_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_admin_audit_event_action",
        "admin_audit_event",
        ["action"],
        unique=False,
    )
    op.create_index(
        "ix_admin_audit_event_request_id",
        "admin_audit_event",
        ["request_id"],
        unique=False,
    )
    op.create_index(
        "ix_admin_audit_event_correlation_id",
        "admin_audit_event",
        ["correlation_id"],
        unique=False,
    )
    op.create_index(
        "ix_admin_audit_event_created_at",
        "admin_audit_event",
        ["created_at"],
        unique=False,
    )
    _install_admin_audit_immutability_guard()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_admin_audit_event_no_truncate "
            "ON admin_audit_event"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_admin_audit_event_immutable "
            "ON admin_audit_event"
        )
        op.execute("DROP FUNCTION IF EXISTS vpn_reject_admin_audit_mutation()")

    op.drop_index("ix_admin_audit_event_created_at", table_name="admin_audit_event")
    op.drop_index("ix_admin_audit_event_correlation_id", table_name="admin_audit_event")
    op.drop_index("ix_admin_audit_event_request_id", table_name="admin_audit_event")
    op.drop_index("ix_admin_audit_event_action", table_name="admin_audit_event")
    op.drop_index("ix_admin_audit_event_actor_user_id", table_name="admin_audit_event")
    op.drop_table("admin_audit_event")

    op.drop_index("ix_admin_session_revoked_at", table_name="admin_session")
    op.drop_index("ix_admin_session_expires_at", table_name="admin_session")
    op.drop_index("ix_admin_session_user_id", table_name="admin_session")
    op.drop_table("admin_session")
    op.drop_table("admin_user")
