from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


class LedgerKind(StrEnum):
    """Business reasons for immutable balance movements."""

    OPENING_BALANCE = "opening_balance"
    MANUAL_TOP_UP = "manual_top_up"
    MANUAL_WITHDRAWAL = "manual_withdrawal"
    ADMIN_ADJUSTMENT = "admin_adjustment"
    PROVIDER_PAYMENT = "provider_payment"
    PERIODIC_CHARGE = "periodic_charge"
    CONFIG_RESERVATION = "config_reservation"
    CONFIG_REFUND = "config_refund"


class LedgerEntry(Base):
    """Append-only record of a committed user balance movement.

    ``balance_after`` makes every entry independently auditable.  Application
    code must only append entries through :class:`BillingRepo`; it deliberately
    exposes no update/delete methods. PostgreSQL also rejects UPDATE and DELETE
    through a migration-installed trigger.
    """

    __tablename__ = "ledger_entry"
    __table_args__ = (
        CheckConstraint("amount <> 0", name="ck_ledger_entry_nonzero_amount"),
        UniqueConstraint("idempotency_key", name="uq_ledger_entry_idempotency_key"),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    reference_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reference_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        index=True,
    )
