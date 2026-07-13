from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from core.config import settings
from core.db.repo.billing import BillingRepo, to_money
from core.exceptions import InvalidOperationError

from .billing_components import (
    BalanceOperations,
    PaidProvisioningOperations,
    PeriodicBillingOperations,
    ProviderPaymentOperations,
)
from .billing_contracts import PaymentIntent, PaymentReceipt
from .config import ConfigService
from .models import User

__all__ = ["BillingService", "PaymentIntent", "PaymentReceipt"]


class BillingService(
    BalanceOperations,
    ProviderPaymentOperations,
    PeriodicBillingOperations,
    PaidProvisioningOperations,
):
    """Stable facade over focused financial application components.

    Existing bot/admin imports deliberately keep using ``BillingService`` while
    the individual use-case groups live in separate modules and can evolve or be
    tested independently.
    """

    def __init__(
        self,
        uow: Callable,
        *,
        per_config_cost: Decimal | int | float | str,
        billing_period_seconds: int | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow = uow
        self._cost = to_money(per_config_cost)
        if self._cost < 0:
            raise InvalidOperationError("Configuration cost cannot be negative")
        self._period_seconds = billing_period_seconds or settings.billing_interval
        if self._period_seconds <= 0:
            raise InvalidOperationError("Billing period must be positive")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._config_service = ConfigService(uow)

    @staticmethod
    def _billing_repo(repos) -> BillingRepo:
        if "billing" in repos:
            return repos["billing"]
        return BillingRepo(repos["users"].session)

    def _billing_period(self, value: datetime) -> tuple[datetime, datetime, str]:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        epoch = int(value.timestamp())
        start_epoch = epoch - epoch % self._period_seconds
        start = datetime.fromtimestamp(start_epoch, tz=timezone.utc)
        end = datetime.fromtimestamp(
            start_epoch + self._period_seconds, tz=timezone.utc
        )
        return start, end, f"v1:{self._period_seconds}:{start_epoch}"

    @staticmethod
    def _positive_money(value: Decimal | int | float | str) -> Decimal:
        amount = to_money(value)
        if amount <= 0:
            raise InvalidOperationError("Amount must be positive")
        return amount

    @staticmethod
    def _billing_notification(user: User, charge: Decimal) -> str | None:
        balance = user.balance
        if balance <= 0:
            return (
                "🔌 Похоже, баланс закончился, и VPN поставлен на паузу.\n"
                "Как только пополните счёт — всё снова заработает. 😉"
            )

        week_high = charge * 24 * 7
        week_low = charge * (24 * 7 - 1)
        day_high = charge * 24
        day_low = charge * 23
        if week_low < balance <= week_high:
            return (
                "🔔 Напоминаем: вашего баланса примерно хватит на неделю.\n"
                "Чтобы избежать перебоев в работе VPN, рекомендуем пополнить "
                "счёт заранее.\n"
                f"💰 Текущий баланс: {user.balance:.2f} руб."
            )
        if day_low < balance <= day_high:
            return (
                "⚠️ Баланса хватит примерно на сутки.\n"
                "Пожалуйста, пополните счёт, чтобы не потерять доступ к VPN.\n"
                f"💰 Текущий баланс: {user.balance:.2f} руб."
            )
        return None
