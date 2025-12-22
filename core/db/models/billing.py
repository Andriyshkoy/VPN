from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base


class BillingSettings(Base):
    """Singleton-style table storing current billing rates."""

    __tablename__ = "billing_settings"

    config_creation_cost: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=10
    )
    monthly_config_cost: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=50
    )
    referral_first_deposit_bonus_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=50
    )
    referral_recurring_bonus_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=10
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), server_default=func.now(), onupdate=func.now()
    )


class BalanceTransaction(Base):
    """Ledger entry for balance changes."""

    __tablename__ = "balance_transaction"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    related_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), nullable=True, index=True
    )
    config_id: Mapped[int | None] = mapped_column(
        ForeignKey("vpn_config.id", ondelete="SET NULL"), nullable=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    kind: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), server_default=func.now(), index=True
    )

    user: Mapped["User"] = relationship(
        "User",
        back_populates="transactions",
        foreign_keys=[user_id],
    )
