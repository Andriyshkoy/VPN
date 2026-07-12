from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


class BillingRun(Base):
    """A uniquely claimed, stable billing period."""

    __tablename__ = "billing_run"
    __table_args__ = (
        CheckConstraint(
            "period_end > period_start", name="ck_billing_run_valid_period"
        ),
    )

    period_key: Mapped[str] = mapped_column(
        String(80), nullable=False, unique=True, index=True
    )
    period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    period_end: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    cost_per_config: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="running", index=True
    )
    charged_users: Mapped[int] = mapped_column(nullable=False, default=0)
    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0.00")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
