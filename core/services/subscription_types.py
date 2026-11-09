"""Public product DTOs. Credentials are only returned by explicit export."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class PlanView:
    code: str
    name: str
    hourly_rate_rub: Decimal
    monthly_price_rub: Decimal
    monthly_estimate_rub: Decimal
    services: tuple[str, ...]
    max_sessions: int | None
    currency: str = "RUB"


PLANS = (
    PlanView(
        "basic",
        "Базовый",
        Decimal("0.07"),
        Decimal("50.00"),
        Decimal("50.40"),
        ("hysteria",),
        1,
    ),
    PlanView(
        "plus",
        "Плюс",
        Decimal("0.14"),
        Decimal("100.00"),
        Decimal("100.80"),
        ("hysteria", "vless-reality"),
        None,
    ),
)


@dataclass(frozen=True, slots=True)
class SubscriptionAccessView:
    service_id: str
    config_id: int | None
    status: str
    remote_access_id: str | None
    generation: int


@dataclass(frozen=True, slots=True)
class SubscriptionView:
    id: str
    user_id: int
    plan_code: str
    status: str
    hourly_rate_rub: Decimal
    monthly_price_rub: Decimal
    monthly_estimate_rub: Decimal
    next_charge_at: datetime | None
    billing_paused: bool
    unlimited_balance: bool
    services: tuple[str, ...]
    accesses: tuple[SubscriptionAccessView, ...]
    version: int
    max_sessions: int | None


@dataclass(frozen=True, slots=True)
class SubscriptionProfile:
    service_id: str
    uri: str = field(repr=False)
