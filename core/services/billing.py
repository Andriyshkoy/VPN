from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable
from uuid import NAMESPACE_URL, uuid4, uuid5

from core.config import settings
from core.db.models.ledger import LedgerKind
from core.db.repo.billing import BillingRepo, to_money
from core.domain import VPNOperationKind, VPNState
from core.exceptions import (
    APIConfigurationError,
    APIRequestRejectedError,
    InvalidOperationError,
)

from .config import ConfigService
from .models import User


@dataclass(frozen=True)
class PaymentIntent:
    intent_id: str
    payload: str
    provider: str
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class PaymentReceipt:
    user: User
    provider: str
    provider_payment_id: str
    credited: bool


class BillingService:
    """Financial use cases backed by an immutable, idempotent ledger."""

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

    async def top_up(
        self,
        user_id: int,
        amount: Decimal | int | float | str,
        *,
        idempotency_key: str | None = None,
    ) -> User:
        """Increase a balance exactly once for a given idempotency key."""

        amount = self._positive_money(amount)
        idempotency_key = idempotency_key or f"manual-top-up:{uuid4()}"
        planned: list[str] = []
        async with self._uow() as repos:
            result = await self._billing_repo(repos).apply_balance_change(
                user_id=user_id,
                amount=amount,
                kind=LedgerKind.MANUAL_TOP_UP,
                idempotency_key=idempotency_key,
                allow_negative_balance=True,
            )
            user = User.from_orm(result.user)
            if user.balance > 0:
                planned = await self._config_service.prepare_entitlement(
                    repos=repos,
                    owner_id=user_id,
                    desired_state=VPNState.ACTIVE.value,
                    kind=VPNOperationKind.UNSUSPEND.value,
                )

        await self._config_service.execute_operations(planned, owner_id=user_id)
        return user

    async def withdraw(
        self,
        user_id: int,
        amount: Decimal | int | float | str,
        *,
        idempotency_key: str | None = None,
    ) -> User:
        """Atomically deduct an amount without allowing an overdraft."""

        amount = self._positive_money(amount)
        idempotency_key = idempotency_key or f"manual-withdrawal:{uuid4()}"
        planned: list[str] = []
        async with self._uow() as repos:
            result = await self._billing_repo(repos).apply_balance_change(
                user_id=user_id,
                amount=-amount,
                kind=LedgerKind.MANUAL_WITHDRAWAL,
                idempotency_key=idempotency_key,
                allow_negative_balance=False,
            )
            user = User.from_orm(result.user)
            if user.balance <= 0:
                planned = await self._config_service.prepare_entitlement(
                    repos=repos,
                    owner_id=user_id,
                    desired_state=VPNState.SUSPENDED.value,
                    kind=VPNOperationKind.SUSPEND.value,
                )

        await self._config_service.execute_operations(planned, owner_id=user_id)
        return user

    async def create_payment_intent(
        self,
        *,
        user_id: int,
        amount: Decimal | int | float | str,
        provider: str = "telegram",
        currency: str = "RUB",
    ) -> PaymentIntent:
        """Create a unique payload to be embedded into a provider invoice."""

        async with self._uow() as repos:
            payment = await self._billing_repo(repos).create_payment_intent(
                user_id=user_id,
                provider=provider,
                amount=amount,
                currency=currency,
            )
            return PaymentIntent(
                intent_id=payment.intent_id,
                payload=payment.payload,
                provider=payment.provider,
                amount=payment.amount,
                currency=payment.currency,
            )

    async def validate_payment_intent(
        self,
        *,
        user_id: int,
        payload: str,
        amount: Decimal | int | float | str,
        currency: str,
        provider: str = "telegram",
    ) -> PaymentIntent:
        """Validate a pending provider invoice before pre-checkout approval."""

        async with self._uow() as repos:
            payment = await self._billing_repo(repos).validate_payment_intent(
                user_id=user_id,
                payload=payload,
                amount=amount,
                currency=currency,
                provider=provider,
            )
            return PaymentIntent(
                intent_id=payment.intent_id,
                payload=payment.payload,
                provider=payment.provider,
                amount=payment.amount,
                currency=payment.currency,
            )

    async def record_provider_payment(
        self,
        *,
        user_id: int,
        provider: str,
        provider_payment_id: str,
        amount: Decimal | int | float | str,
        currency: str,
        payload: str,
        intent_id: str | None = None,
        raw_data: dict | None = None,
    ) -> PaymentReceipt:
        """Validate, persist and credit a provider payment atomically."""

        planned: list[str] = []
        async with self._uow() as repos:
            result = await self._billing_repo(repos).record_provider_payment(
                user_id=user_id,
                provider=provider,
                provider_payment_id=provider_payment_id,
                amount=amount,
                currency=currency,
                payload=payload,
                intent_id=intent_id,
                raw_data=raw_data,
            )
            user = User.from_orm(result.user)
            if user.balance > 0:
                planned = await self._config_service.prepare_entitlement(
                    repos=repos,
                    owner_id=user_id,
                    desired_state=VPNState.ACTIVE.value,
                    kind=VPNOperationKind.UNSUSPEND.value,
                )

        await self._config_service.execute_operations(planned, owner_id=user_id)
        return PaymentReceipt(
            user=user,
            provider=provider,
            provider_payment_id=provider_payment_id,
            credited=result.credited,
        )

    async def record_telegram_payment(
        self,
        *,
        user_id: int,
        telegram_payment_charge_id: str,
        total_amount_minor: int,
        currency: str,
        payload: str,
        intent_id: str | None = None,
        provider_payment_charge_id: str | None = None,
        raw_data: dict | None = None,
    ) -> PaymentReceipt:
        """Record Telegram's integer-minor-unit successful-payment update."""

        if isinstance(total_amount_minor, bool) or not isinstance(
            total_amount_minor, int
        ):
            raise InvalidOperationError("Telegram amount must use integer minor units")
        if total_amount_minor <= 0:
            raise InvalidOperationError("Amount must be positive")
        amount = (Decimal(total_amount_minor) / Decimal(100)).quantize(Decimal("0.01"))
        provider_id = telegram_payment_charge_id.strip()
        raw = dict(raw_data or {})
        if provider_payment_charge_id:
            raw.setdefault("provider_payment_charge_id", provider_payment_charge_id)
        return await self.record_provider_payment(
            user_id=user_id,
            provider="telegram",
            provider_payment_id=provider_id,
            amount=amount,
            currency=currency,
            payload=payload,
            intent_id=intent_id,
            raw_data=raw,
        )

    async def charge_all(
        self, *, period_key: str | None = None, at: datetime | None = None
    ) -> dict[User, Decimal]:
        """Charge one stable period, at most once across concurrent workers."""

        if settings.maintenance_mode or not settings.billing_enabled:
            return {}

        start, end, default_key = self._billing_period(at or self._clock())
        effective_period_key = period_key or default_key
        planned: list[str] = []
        async with self._uow() as repos:
            billing_repo = self._billing_repo(repos)
            results = await billing_repo.charge_period(
                period_key=effective_period_key,
                period_start=start,
                period_end=end,
                cost_per_config=self._cost,
            )
            charged: dict[User, Decimal] = {}
            if settings.notifications_enabled:
                notification_candidates = []
            else:
                notification_candidates = None
            for result in results:
                user = User.from_orm(result.user)
                charged[user] = result.amount
                if user.balance <= 0:
                    planned.extend(
                        await self._config_service.prepare_entitlement(
                            repos=repos,
                            owner_id=user.id,
                            desired_state=VPNState.SUSPENDED.value,
                            kind=VPNOperationKind.SUSPEND.value,
                        )
                    )
                if (
                    notification_candidates is not None
                    and result.user.telegram_delivery_status == "active"
                ):
                    notification_candidates.append((user, result.amount))

            if notification_candidates is not None:
                for user, charge in notification_candidates:
                    text = self._billing_notification(user, charge)
                    if text is not None:
                        await billing_repo.add_notification_outbox(
                            dedupe_key=(
                                f"billing-notification:{effective_period_key}:"
                                f"user:{user.id}"
                            ),
                            chat_id=user.tg_id,
                            text=text,
                        )

        await self._config_service.execute_operations(planned)
        return charged

    async def reconcile_pending_config_operations(
        self, *, limit: int = 100
    ) -> tuple[dict[int, str], int]:
        """Converge ambiguous Manager operations and settle rejected reserves."""

        lifecycle = await self._config_service.reconcile(limit=limit)
        async with self._uow() as repos:
            refunds = await self._billing_repo(
                repos
            ).refund_rejected_config_reservations()
        return lifecycle, refunds

    async def create_paid_config(
        self,
        *,
        server_id: int,
        owner_id: int,
        name: str,
        display_name: str,
        creation_cost: Decimal | int | float | str,
        use_password: bool = False,
        idempotency_key: str | None = None,
    ) -> "Config":  # type: ignore[valid-type] # noqa: F821
        """Atomically reserve funds and persist a recoverable provision intent.

        The database transaction is complete before Manager I/O starts.  A process
        crash therefore leaves either no debit at all or a debit linked to an
        immutable provision operation that reconciliation can finish or refund.
        """

        if settings.maintenance_mode or not settings.provisioning_enabled:
            raise InvalidOperationError(
                "VPN configuration provisioning is temporarily disabled"
            )

        creation_cost = to_money(creation_cost)
        if creation_cost < 0:
            raise InvalidOperationError("Configuration cost cannot be negative")

        if idempotency_key is None:
            operation_id = str(uuid4())
        else:
            if (
                not isinstance(idempotency_key, str)
                or not idempotency_key.strip()
                or len(idempotency_key) > 160
            ):
                raise InvalidOperationError("Invalid idempotency key")
            # Scope keys per account: clients may naturally reuse the same token
            # for unrelated accounts, while reuse by one account must resolve to
            # the same immutable provision operation.
            operation_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"vpn-paid-config:{owner_id}:{idempotency_key.strip()}",
                )
            )

        reservation_key = f"config-reservation:{operation_id}"
        async with self._uow() as repos:
            context = await self._config_service.prepare_config(
                repos=repos,
                operation_id=operation_id,
                server_id=server_id,
                owner_id=owner_id,
                name=name,
                display_name=display_name,
                use_password=use_password,
            )
            if creation_cost > 0:
                await self._billing_repo(repos).apply_balance_change(
                    user_id=owner_id,
                    amount=-creation_cost,
                    kind=LedgerKind.CONFIG_RESERVATION,
                    idempotency_key=reservation_key,
                    allow_negative_balance=False,
                    reference_type="vpn_operation",
                    reference_id=operation_id,
                    details={"server_id": server_id, "config_name": name},
                )

        try:
            return await self._config_service.execute_prepared(context)
        except (APIConfigurationError, APIRequestRejectedError):
            # ConfigService persists REJECTED before re-raising.  Only the exact
            # rejected PROVISION operation is eligible for compensation; transport
            # and local persistence failures stay reserved for reconciliation.
            if creation_cost > 0:
                async with self._uow() as repos:
                    await self._billing_repo(repos).refund_rejected_config_reservations(
                        operation_id=operation_id
                    )
            raise

    @staticmethod
    def _billing_repo(repos) -> BillingRepo:
        # The fallback keeps third-party/custom UoWs compatible while the central
        # UoW is migrated to expose ``repos["billing"]`` explicitly.
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
        """Build the existing user-facing notice inside the billing UoW."""

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
