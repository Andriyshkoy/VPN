from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base
from core.domain.telegram import TelegramUpdateStatus


class TelegramUpdateInbox(Base):
    """Durable source of truth for updates accepted from Telegram.

    Telegram considers a long-polled update acknowledged once a later
    ``getUpdates`` request advances the offset. Persisting the complete payload
    here before advancing that offset closes the process-crash loss window.
    """

    __tablename__ = "telegram_update_inbox"
    __table_args__ = (
        CheckConstraint(
            "update_id >= 0", name="ck_telegram_update_inbox_nonnegative_update_id"
        ),
        CheckConstraint(
            "attempts >= 0", name="ck_telegram_update_inbox_nonnegative_attempts"
        ),
        CheckConstraint(
            "source IN ('polling', 'webhook')",
            name="ck_telegram_update_inbox_source",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'failed', 'processed', 'dead')",
            name="ck_telegram_update_inbox_status",
        ),
        Index(
            "ix_telegram_update_inbox_status_next_attempt_at",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_telegram_update_inbox_status_updated_at",
            "status",
            "updated_at",
        ),
        Index(
            "ix_telegram_update_inbox_ordering_status_update",
            "ordering_key",
            "status",
            "update_id",
        ),
    )

    update_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, unique=True, index=True
    )
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    # HMAC-derived conversation lane; contains no raw Telegram identifier.
    ordering_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default=TelegramUpdateStatus.PENDING.value,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        index=True,
    )
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
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
        onupdate=func.now(),
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
