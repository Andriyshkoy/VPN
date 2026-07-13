from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


class ProviderPayment(Base):
    """Payment intent and its eventual provider confirmation.

    The provider charge identifier is unique per provider.  This is the final
    database-level guard against Telegram (or another provider) delivering the
    same successful payment more than once.
    """

    __tablename__ = "provider_payment"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_provider_payment_positive_amount"),
        CheckConstraint(
            "referral_settlement_status IS NULL OR "
            "referral_settlement_status IN "
            "('rewarded', 'no_referrer', 'zero_reward', 'invalid_chain', "
            "'invalid_accounting')",
            name="ck_provider_payment_referral_settlement_status",
        ),
        CheckConstraint(
            "(referral_settled_at IS NULL AND referral_program_version IS NULL "
            "AND referral_settlement_status IS NULL) OR "
            "(status = 'credited' AND ledger_entry_id IS NOT NULL "
            "AND referral_settled_at IS NOT NULL "
            "AND referral_program_version IS NOT NULL "
            "AND referral_settlement_status IS NOT NULL)",
            name="ck_provider_payment_referral_settlement_complete",
        ),
        UniqueConstraint(
            "provider",
            "provider_payment_id",
            name="uq_provider_payment_provider_charge",
        ),
        Index(
            "ix_provider_payment_referral_settlement",
            "status",
            "referral_settled_at",
            "id",
        ),
    )

    intent_id: Mapped[str] = mapped_column(
        String(36), nullable=False, unique=True, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_payment_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    payload: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", index=True
    )
    ledger_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("ledger_entry.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )
    raw_data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc) + timedelta(hours=1),
        index=True,
    )
    credited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    referral_settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    referral_program_version: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    referral_settlement_status: Mapped[str | None] = mapped_column(
        String(24), nullable=True
    )
