"""Durable, reviewed administrative message campaigns.

Recipient and message snapshots are immutable once a campaign is launched.  A
separate operation log makes old HTTP idempotency keys safe even after retries.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


class AdminCampaign(Base):
    __tablename__ = "admin_campaign"
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_admin_campaign_public_id"),
        CheckConstraint(
            "status IN ('draft','queued','running','completed','cancelled')",
            name="ck_admin_campaign_status",
        ),
        CheckConstraint("revision >= 1", name="ck_admin_campaign_revision"),
    )

    public_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    audience: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="draft", index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_user.id", ondelete="SET NULL"), nullable=True
    )
    preview_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preview_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preview_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    previewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )
    launched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CampaignRecipient(Base):
    __tablename__ = "campaign_recipient"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "user_id", name="uq_campaign_recipient_campaign_user"
        ),
        UniqueConstraint("outbox_dedupe_key", name="uq_campaign_recipient_dedupe"),
        CheckConstraint(
            "status IN ('pending','queued','sending','delivered','failed','unknown','skipped','cancelled')",
            name="ck_campaign_recipient_status",
        ),
        CheckConstraint(
            "attempts >= 0 AND generation >= 0", name="ck_campaign_recipient_attempts"
        ),
        Index(
            "ix_campaign_recipient_campaign_status_id", "campaign_id", "status", "id"
        ),
    )

    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("admin_campaign.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    tg_id_snapshot: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", index=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outbox_dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False)
    outbox_id: Mapped[int | None] = mapped_column(
        ForeignKey("notification_outbox.id", ondelete="SET NULL"), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(96), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )


class CampaignOperation(Base):
    __tablename__ = "campaign_operation"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id", "kind", "key_hash", name="uq_campaign_operation_idempotency"
        ),
        CheckConstraint(
            "kind IN ('launch','retry')", name="ck_campaign_operation_kind"
        ),
    )

    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("admin_campaign.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    recipient_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )
