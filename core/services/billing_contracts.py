from dataclasses import dataclass
from decimal import Decimal

from .models import User


@dataclass(frozen=True)
class PaymentIntent:
    """Provider-neutral invoice intent exposed by the application layer."""

    intent_id: str
    payload: str
    provider: str
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class PaymentReceipt:
    """Result of idempotently recording a captured provider payment."""

    user: User
    provider: str
    provider_payment_id: str
    credited: bool
