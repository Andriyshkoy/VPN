from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base

if TYPE_CHECKING:
    from core.db.models.ledger import LedgerEntry
    from core.db.models.payment import ProviderPayment
    from core.db.models.user import User


class ReferralReward(Base):
    """Immutable audit record for one level of a referral payment reward."""

    __tablename__ = "referral_reward"
    __table_args__ = (
        CheckConstraint("level IN (1, 2)", name="ck_referral_reward_level"),
        CheckConstraint(
            "rate_bps > 0 AND rate_bps <= 10000",
            name="ck_referral_reward_rate_bps",
        ),
        CheckConstraint(
            "source_amount > 0", name="ck_referral_reward_positive_source_amount"
        ),
        CheckConstraint(
            "reward_amount > 0", name="ck_referral_reward_positive_reward_amount"
        ),
        CheckConstraint(
            "source_user_id <> beneficiary_user_id",
            name="ck_referral_reward_not_self",
        ),
        UniqueConstraint(
            "source_payment_id",
            "level",
            name="uq_referral_reward_payment_level",
        ),
        UniqueConstraint(
            "source_payment_id",
            "beneficiary_user_id",
            name="uq_referral_reward_payment_beneficiary",
        ),
        UniqueConstraint("ledger_entry_id", name="uq_referral_reward_ledger_entry_id"),
    )

    source_payment_id: Mapped[int] = mapped_column(
        ForeignKey("provider_payment.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    beneficiary_user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    rate_bps: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    source_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    reward_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    ledger_entry_id: Mapped[int] = mapped_column(
        ForeignKey("ledger_entry.id", ondelete="RESTRICT"),
        nullable=False,
    )
    program_version: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="v1-5pct-1pct",
        server_default="v1-5pct-1pct",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        index=True,
    )

    source_payment: Mapped["ProviderPayment"] = relationship("ProviderPayment")
    source_user: Mapped["User"] = relationship("User", foreign_keys=[source_user_id])
    beneficiary_user: Mapped["User"] = relationship(
        "User", foreign_keys=[beneficiary_user_id]
    )
    ledger_entry: Mapped["LedgerEntry"] = relationship("LedgerEntry")
