from __future__ import annotations

import secrets
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base


class User(Base):

    __table_args__ = (
        CheckConstraint(
            "referred_by_id IS NULL OR referred_by_id <> id",
            name="ck_user_not_self_referred",
        ),
    )

    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    referral_code: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        unique=True,
        index=True,
        default=lambda: secrets.token_urlsafe(24),
    )

    created: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), server_default=func.now()
    )

    balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)

    telegram_delivery_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", index=True
    )
    telegram_blocked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    telegram_last_delivery_error: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    telegram_delivery_status_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )

    vpn_configs: Mapped[list["VPN_Config"]] = relationship(  # noqa F821 # type: ignore
        "VPN_Config",
        back_populates="owner",
        passive_deletes=True,
    )
    # Кто пригласил этого пользователя
    referred_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id"), nullable=True, default=None, index=True
    )
    referred_by: Mapped["User | None"] = relationship(
        back_populates="referrals", remote_side="User.id"
    )

    # Кого пригласил этот пользователь
    referrals: Mapped[list["User"]] = relationship(back_populates="referred_by")
