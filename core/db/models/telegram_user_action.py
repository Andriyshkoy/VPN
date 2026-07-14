from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


class TelegramUserActionEvent(Base):
    """Append-only, privacy-safe record of an input handled by the bot.

    The raw Telegram update remains in the short-lived inbox only.  This table
    deliberately stores a small allowlisted classification and never message
    text, callback data, payment payloads, config contents, or Telegram API
    credentials.
    """

    __tablename__ = "telegram_user_action_event"
    __table_args__ = (
        CheckConstraint(
            "source_update_id >= 0",
            name="ck_telegram_user_action_nonnegative_update_id",
        ),
        CheckConstraint(
            "category = 'bot'",
            name="ck_telegram_user_action_category",
        ),
        CheckConstraint(
            "result IN ('handled', 'completed', 'rejected', 'ignored', "
            "'invalid', 'unavailable', 'failed')",
            name="ck_telegram_user_action_result",
        ),
        UniqueConstraint(
            "source_update_id",
            name="uq_telegram_user_action_source_update_id",
        ),
        Index(
            "ix_telegram_user_action_user_occurred",
            "user_id",
            "occurred_at",
            "id",
        ),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_update_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(
        String(16), nullable=False, default="bot", server_default="bot"
    )
    action: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    result: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    metadata_json: Mapped[dict] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        index=True,
    )
