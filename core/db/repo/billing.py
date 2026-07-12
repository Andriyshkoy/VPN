from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Sequence
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from core.config import settings
from core.db.models.billing_run import BillingRun
from core.db.models.config import VPN_Config
from core.db.models.ledger import LedgerEntry, LedgerKind
from core.db.models.notification_outbox import NotificationOutbox
from core.db.models.payment import ProviderPayment
from core.db.models.user import User
from core.db.models.vpn_operation import VPNOperation
from core.domain import VPNOperationKind, VPNState
from core.domain.vpn import VPNOperationStatus
from core.exceptions import (
    InsufficientBalanceError,
    InvalidOperationError,
    UserNotFoundError,
)

MONEY_QUANTUM = Decimal("0.01")


def to_money(value: Decimal | int | float | str) -> Decimal:
    """Convert external values without carrying binary-float artefacts."""

    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
        if not decimal_value.is_finite():
            raise InvalidOperation
        return decimal_value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InvalidOperationError("Invalid monetary amount") from exc


@dataclass(frozen=True)
class BalanceChangeResult:
    user: User
    ledger_entry: LedgerEntry
    applied: bool


@dataclass(frozen=True)
class PeriodChargeResult:
    user: User
    amount: Decimal
    ledger_entry: LedgerEntry


@dataclass(frozen=True)
class PaymentCreditResult:
    user: User
    payment: ProviderPayment
    ledger_entry: LedgerEntry
    credited: bool


class BillingRepo:
    """Atomic persistence primitives for balance-changing operations.

    Every balance update and its ledger entry are flushed in the caller's same
    transaction.  Atomic SQL arithmetic prevents read/modify/write lost updates.
    """

    def __init__(self, session) -> None:
        self.session = session

    async def get_ledger_entry(self, idempotency_key: str) -> LedgerEntry | None:
        return await self.session.scalar(
            select(LedgerEntry).where(LedgerEntry.idempotency_key == idempotency_key)
        )

    async def list_ledger_entries(
        self, user_id: int, *, limit: int = 100, offset: int = 0
    ) -> Sequence[LedgerEntry]:
        entries = await self.session.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.user_id == user_id)
            .order_by(LedgerEntry.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return entries.all()

    async def apply_balance_change(
        self,
        *,
        user_id: int,
        amount: Decimal | int | float | str,
        kind: LedgerKind | str,
        idempotency_key: str,
        allow_negative_balance: bool,
        reference_type: str | None = None,
        reference_id: str | None = None,
        details: dict | None = None,
    ) -> BalanceChangeResult:
        normalized_amount = to_money(amount)
        if normalized_amount == 0:
            raise InvalidOperationError("Balance movement must be non-zero")
        if not idempotency_key or len(idempotency_key) > 160:
            raise InvalidOperationError("Invalid idempotency key")

        # Serialize a user's ledger so ``balance_after`` and entry order remain
        # auditable under concurrent top-up/charge/withdraw operations.
        locked_user = await self.session.scalar(
            select(User).where(User.id == user_id).with_for_update()
        )
        if locked_user is None:
            raise UserNotFoundError(f"User with ID {user_id} not found")

        kind_value = kind.value if isinstance(kind, LedgerKind) else str(kind)
        existing = await self.get_ledger_entry(idempotency_key)
        if existing is not None:
            self._validate_existing_entry(
                existing,
                user_id=user_id,
                amount=normalized_amount,
                kind=kind_value,
            )
            return BalanceChangeResult(
                user=locked_user,
                ledger_entry=existing,
                applied=False,
            )

        conditions = [User.id == user_id]
        if normalized_amount < 0 and not allow_negative_balance:
            conditions.append(User.balance >= -normalized_amount)

        try:
            # The balance update and final immutable ledger row share a
            # savepoint. A cross-user collision on the globally unique key
            # rolls both back before the existing entry is validated.
            async with self.session.begin_nested():
                updated = await self.session.scalar(
                    update(User)
                    .where(*conditions)
                    .values(balance=User.balance + normalized_amount)
                    .returning(User)
                )
                if updated is None:
                    raise InsufficientBalanceError("Insufficient balance")

                entry = LedgerEntry(
                    user_id=user_id,
                    amount=normalized_amount,
                    balance_after=updated.balance,
                    kind=kind_value,
                    idempotency_key=idempotency_key,
                    reference_type=reference_type,
                    reference_id=reference_id,
                    details=details or {},
                )
                self.session.add(entry)
                await self.session.flush()
        except IntegrityError:
            await self.session.refresh(locked_user)
            existing = await self.get_ledger_entry(idempotency_key)
            if existing is None:
                raise
            self._validate_existing_entry(
                existing,
                user_id=user_id,
                amount=normalized_amount,
                kind=kind_value,
            )
            return BalanceChangeResult(
                user=locked_user,
                ledger_entry=existing,
                applied=False,
            )

        return BalanceChangeResult(user=updated, ledger_entry=entry, applied=True)

    async def create_payment_intent(
        self,
        *,
        user_id: int,
        provider: str,
        amount: Decimal | int | float | str,
        currency: str,
        intent_id: str | None = None,
    ) -> ProviderPayment:
        normalized_amount = self._positive_amount(amount)
        provider = self._provider(provider)
        currency = self._currency(currency)
        if await self.session.get(User, user_id) is None:
            raise UserNotFoundError(f"User with ID {user_id} not found")

        intent_id = intent_id or str(uuid4())
        if len(intent_id) > 36:
            raise InvalidOperationError("Invalid payment intent ID")
        payment = ProviderPayment(
            intent_id=intent_id,
            user_id=user_id,
            provider=provider,
            amount=normalized_amount,
            currency=currency,
            payload=f"topup:{intent_id}",
            status="pending",
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=settings.payment_intent_ttl_seconds),
        )
        self.session.add(payment)
        await self.session.flush()
        return payment

    async def validate_payment_intent(
        self,
        *,
        user_id: int,
        payload: str,
        amount: Decimal | int | float | str,
        currency: str,
        provider: str,
    ) -> ProviderPayment:
        """Validate a pending invoice before the provider captures funds."""

        normalized_amount = self._positive_amount(amount)
        currency = self._currency(currency)
        provider = self._provider(provider)
        if not payload.startswith("topup:") or len(payload) > 200:
            raise InvalidOperationError("Payment intent not found")

        payment = await self.session.scalar(
            select(ProviderPayment)
            .where(
                ProviderPayment.user_id == user_id,
                ProviderPayment.payload == payload,
            )
            .order_by(ProviderPayment.id.desc())
        )
        if payment is None or payment.status != "pending":
            raise InvalidOperationError("Payment intent not found")
        self._ensure_intent_active(payment)
        if payment.provider != provider:
            raise InvalidOperationError("Payment provider mismatch")
        self._validate_payment(
            payment,
            user_id=user_id,
            amount=normalized_amount,
            currency=currency,
            payload=payload,
            require_payload=True,
        )
        return payment

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
    ) -> PaymentCreditResult:
        normalized_amount = self._positive_amount(amount)
        provider = self._provider(provider)
        currency = self._currency(currency)
        provider_payment_id = provider_payment_id.strip()
        if not provider_payment_id or len(provider_payment_id) > 160:
            raise InvalidOperationError("Invalid provider payment ID")
        if not payload or len(payload) > 200:
            raise InvalidOperationError("Invalid payment payload")

        existing = await self._get_provider_payment(provider, provider_payment_id)
        if existing is not None:
            if intent_id is not None and existing.intent_id != intent_id:
                raise InvalidOperationError(
                    "Provider payment ID belongs to a different intent"
                )
            return await self._existing_payment_result(
                existing,
                user_id=user_id,
                amount=normalized_amount,
                currency=currency,
                payload=payload,
            )

        if intent_id is not None:
            payment = await self.session.scalar(
                select(ProviderPayment)
                .where(ProviderPayment.intent_id == intent_id)
                .with_for_update()
            )
            if payment is None:
                raise InvalidOperationError("Payment intent not found")
            self._validate_payment(
                payment,
                user_id=user_id,
                amount=normalized_amount,
                currency=currency,
                payload=payload,
                require_payload=True,
            )
            if payment.status == "credited":
                if payment.provider_payment_id != provider_payment_id:
                    raise InvalidOperationError(
                        "Payment intent was already credited by another charge"
                    )
                return await self._existing_payment_result(
                    payment,
                    user_id=user_id,
                    amount=normalized_amount,
                    currency=currency,
                    payload=payload,
                )
            self._ensure_intent_active(payment)
            if payment.provider != provider:
                raise InvalidOperationError("Payment provider mismatch")
            try:
                async with self.session.begin_nested():
                    payment.provider_payment_id = provider_payment_id
                    payment.raw_data = raw_data or {}
                    await self.session.flush()
            except IntegrityError:
                other = await self._get_provider_payment(provider, provider_payment_id)
                if other is None:
                    raise
                if other.intent_id != intent_id:
                    raise InvalidOperationError(
                        "Provider payment ID belongs to a different intent"
                    )
                return await self._existing_payment_result(
                    other,
                    user_id=user_id,
                    amount=normalized_amount,
                    currency=currency,
                    payload=payload,
                )
        else:
            payment = ProviderPayment(
                intent_id=str(uuid4()),
                user_id=user_id,
                provider=provider,
                provider_payment_id=provider_payment_id,
                amount=normalized_amount,
                currency=currency,
                payload=payload,
                status="pending",
                raw_data=raw_data or {},
            )
            try:
                async with self.session.begin_nested():
                    self.session.add(payment)
                    await self.session.flush()
            except IntegrityError:
                existing = await self._get_provider_payment(
                    provider, provider_payment_id
                )
                if existing is None:
                    raise
                return await self._existing_payment_result(
                    existing,
                    user_id=user_id,
                    amount=normalized_amount,
                    currency=currency,
                    payload=payload,
                )

        ledger_result = await self.apply_balance_change(
            user_id=user_id,
            amount=normalized_amount,
            kind=LedgerKind.PROVIDER_PAYMENT,
            idempotency_key=(f"provider-payment:{provider}:{payment.intent_id}"),
            allow_negative_balance=True,
            reference_type="provider_payment",
            reference_id=str(payment.id),
            details={
                "provider": provider,
                "provider_payment_id": provider_payment_id,
                "currency": currency,
            },
        )
        payment.status = "credited"
        payment.ledger_entry_id = ledger_result.ledger_entry.id
        payment.credited_at = datetime.now(timezone.utc)
        await self.session.flush()
        return PaymentCreditResult(
            user=ledger_result.user,
            payment=payment,
            ledger_entry=ledger_result.ledger_entry,
            credited=ledger_result.applied,
        )

    async def charge_period(
        self,
        *,
        period_key: str,
        period_start: datetime,
        period_end: datetime,
        cost_per_config: Decimal | int | float | str,
    ) -> list[PeriodChargeResult]:
        cost = to_money(cost_per_config)
        if cost < 0:
            raise InvalidOperationError("Configuration cost cannot be negative")
        if not period_key or len(period_key) > 80:
            raise InvalidOperationError("Invalid billing period key")
        if period_end <= period_start:
            raise InvalidOperationError("Invalid billing period")

        run, claimed = await self._claim_billing_run(
            period_key=period_key,
            period_start=period_start,
            period_end=period_end,
            cost_per_config=cost,
        )
        if not claimed:
            self._validate_existing_run(
                run,
                period_start=period_start,
                period_end=period_end,
                cost_per_config=cost,
            )
            return []

        if cost == 0:
            await self._complete_billing_run(run, charged_users=0, total=Decimal(0))
            return []

        active_counts = await self.session.execute(
            select(VPN_Config.owner_id, func.count(VPN_Config.id))
            .where(VPN_Config.actual_state == VPNState.ACTIVE.value)
            .group_by(VPN_Config.owner_id)
            .order_by(VPN_Config.owner_id)
        )

        charged: list[PeriodChargeResult] = []
        total = Decimal("0.00")
        for user_id, config_count in active_counts:
            charge = to_money(cost * int(config_count))
            if charge == 0:
                continue
            movement = await self.apply_balance_change(
                user_id=user_id,
                amount=-charge,
                kind=LedgerKind.PERIODIC_CHARGE,
                idempotency_key=f"billing:{period_key}:user:{user_id}",
                allow_negative_balance=True,
                reference_type="billing_run",
                reference_id=str(run.id),
                details={
                    "period_key": period_key,
                    "config_count": int(config_count),
                    "cost_per_config": str(cost),
                },
            )
            if movement.applied:
                charged.append(
                    PeriodChargeResult(
                        user=movement.user,
                        amount=charge,
                        ledger_entry=movement.ledger_entry,
                    )
                )
                total += charge

        await self._complete_billing_run(run, charged_users=len(charged), total=total)
        return charged

    async def add_notification_outbox(
        self,
        *,
        dedupe_key: str,
        chat_id: int,
        text: str,
    ) -> NotificationOutbox:
        """Persist a billing notification in the caller's transaction."""

        if not dedupe_key or len(dedupe_key) > 160:
            raise InvalidOperationError("Invalid notification idempotency key")
        if not text:
            raise InvalidOperationError("Notification text must not be empty")
        item = NotificationOutbox(
            dedupe_key=dedupe_key,
            chat_id=chat_id,
            text=text,
            status="pending",
        )
        self.session.add(item)
        await self.session.flush()
        return item

    async def claim_notification_outbox(
        self,
        *,
        limit: int = 100,
        now: datetime | None = None,
    ) -> Sequence[NotificationOutbox]:
        """Lock a fair page while it is published to the durable Redis queue."""

        now = now or datetime.now(timezone.utc)
        stale_before = now - timedelta(seconds=settings.notification_visibility_timeout)
        items = await self.session.scalars(
            select(NotificationOutbox)
            .where(
                or_(
                    and_(
                        NotificationOutbox.status == "pending",
                        NotificationOutbox.next_attempt_at <= now,
                    ),
                    and_(
                        NotificationOutbox.status == "queued",
                        NotificationOutbox.published_at <= stale_before,
                    ),
                )
            )
            .order_by(
                NotificationOutbox.next_attempt_at,
                NotificationOutbox.published_at,
                NotificationOutbox.id,
            )
            .with_for_update(skip_locked=True)
            .limit(limit)
        )
        return items.all()

    async def mark_notification_published(
        self,
        item: NotificationOutbox,
        *,
        now: datetime | None = None,
    ) -> None:
        item.status = "queued"
        item.published_at = now or datetime.now(timezone.utc)
        item.last_error = None
        await self.session.flush()

    async def mark_notification_retry(
        self,
        item: NotificationOutbox,
        error: str,
        *,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(timezone.utc)
        item.attempts += 1
        item.status = "pending"
        item.published_at = None
        delay_seconds = min(3600, 2 ** min(item.attempts, 10))
        item.next_attempt_at = now + timedelta(seconds=delay_seconds)
        item.last_error = error[:4000]
        await self.session.flush()

    async def settle_notification_outbox(
        self,
        *,
        dedupe_key: str,
        delivered: bool,
        error: str | None = None,
    ) -> bool:
        """Record the Telegram consumer's terminal outcome in PostgreSQL."""

        result = await self.session.execute(
            update(NotificationOutbox)
            .where(
                NotificationOutbox.dedupe_key == dedupe_key,
                NotificationOutbox.status.in_({"pending", "queued"}),
            )
            .values(
                status="delivered" if delivered else "failed",
                last_error=None if delivered else (error or "delivery failed")[:4000],
            )
        )
        return bool(result.rowcount)

    async def touch_notification_outbox(
        self,
        *,
        dedupe_key: str,
        now: datetime | None = None,
    ) -> bool:
        """Extend visibility while Telegram has scheduled a delayed retry."""

        result = await self.session.execute(
            update(NotificationOutbox)
            .where(
                NotificationOutbox.dedupe_key == dedupe_key,
                NotificationOutbox.status.in_({"pending", "queued"}),
            )
            .values(
                status="queued",
                published_at=now or datetime.now(timezone.utc),
            )
        )
        return bool(result.rowcount)

    async def refund_rejected_config_reservations(
        self, *, operation_id: str | None = None
    ) -> int:
        """Compensate reservations whose durable provisioning was rejected.

        A transport timeout intentionally keeps funds reserved. If a later
        reconciliation receives a definitive rejection, this query finds the
        matching reservation and refunds it exactly once.  The reservation is
        linked to the immutable provision operation ID rather than to a config's
        mutable ``operation_id`` pointer, which is replaced by every later
        suspend/unsuspend/revoke transition.
        """

        stmt = (
            select(LedgerEntry)
            .join(
                VPNOperation,
                VPNOperation.operation_id == LedgerEntry.reference_id,
            )
            .where(
                LedgerEntry.kind == LedgerKind.CONFIG_RESERVATION.value,
                LedgerEntry.reference_type == "vpn_operation",
                LedgerEntry.amount < 0,
                VPNOperation.kind == VPNOperationKind.PROVISION.value,
                VPNOperation.status == VPNOperationStatus.REJECTED.value,
            )
            .order_by(LedgerEntry.id)
        )
        if operation_id is not None:
            stmt = stmt.where(VPNOperation.operation_id == operation_id)

        reservations = await self.session.scalars(stmt)
        refunded = 0
        for reservation in reservations:
            result = await self.apply_balance_change(
                user_id=reservation.user_id,
                amount=-reservation.amount,
                kind=LedgerKind.CONFIG_REFUND,
                idempotency_key=f"config-refund:{reservation.idempotency_key}",
                allow_negative_balance=True,
                reference_type="vpn_operation",
                reference_id=reservation.reference_id,
                details={"reservation_key": reservation.idempotency_key},
            )
            refunded += int(result.applied)
        return refunded

    async def _claim_billing_run(
        self,
        *,
        period_key: str,
        period_start: datetime,
        period_end: datetime,
        cost_per_config: Decimal,
    ) -> tuple[BillingRun, bool]:
        # Changing BILLING_INTERVAL must not create a second run whose wall-clock
        # window overlaps a previously charged one. PostgreSQL workers serialize
        # this schedule check with one transaction-scoped advisory lock.
        bind = self.session.get_bind()
        if bind.dialect.name == "postgresql":
            await self.session.execute(
                select(func.pg_advisory_xact_lock(1_984_110_711))
            )

        existing = await self.session.scalar(
            select(BillingRun).where(BillingRun.period_key == period_key)
        )
        if existing is not None:
            return existing, False

        overlap = await self.session.scalar(
            select(BillingRun.id)
            .where(
                BillingRun.period_start < period_end,
                BillingRun.period_end > period_start,
            )
            .limit(1)
        )
        if overlap is not None:
            raise InvalidOperationError(
                "Billing period overlaps an already claimed wall-clock window"
            )

        run = BillingRun(
            period_key=period_key,
            period_start=period_start,
            period_end=period_end,
            cost_per_config=cost_per_config,
            status="running",
        )
        try:
            async with self.session.begin_nested():
                self.session.add(run)
                await self.session.flush()
            return run, True
        except IntegrityError:
            existing = await self.session.scalar(
                select(BillingRun).where(BillingRun.period_key == period_key)
            )
            if existing is None:
                raise
            return existing, False

    async def _complete_billing_run(
        self, run: BillingRun, *, charged_users: int, total: Decimal
    ) -> None:
        run.status = "completed"
        run.charged_users = charged_users
        run.total_amount = to_money(total)
        run.completed_at = datetime.now(timezone.utc)
        await self.session.flush()

    async def _get_provider_payment(
        self, provider: str, provider_payment_id: str
    ) -> ProviderPayment | None:
        return await self.session.scalar(
            select(ProviderPayment).where(
                ProviderPayment.provider == provider,
                ProviderPayment.provider_payment_id == provider_payment_id,
            )
        )

    async def _existing_payment_result(
        self,
        payment: ProviderPayment,
        *,
        user_id: int,
        amount: Decimal,
        currency: str,
        payload: str,
    ) -> PaymentCreditResult:
        self._validate_payment(
            payment,
            user_id=user_id,
            amount=amount,
            currency=currency,
            payload=payload,
            # Legacy Telegram invoices used a shared ``topup`` payload.  A new
            # intent, when supplied, is validated strictly above.
            require_payload=False,
        )
        if payment.status != "credited" or payment.ledger_entry_id is None:
            raise InvalidOperationError("Payment is not credited")
        ledger_entry = await self.session.get(LedgerEntry, payment.ledger_entry_id)
        user = await self.session.get(User, user_id)
        if ledger_entry is None or user is None:
            raise InvalidOperationError("Payment accounting record is incomplete")
        return PaymentCreditResult(
            user=user,
            payment=payment,
            ledger_entry=ledger_entry,
            credited=False,
        )

    @staticmethod
    def _validate_existing_entry(
        entry: LedgerEntry,
        *,
        user_id: int,
        amount: Decimal,
        kind: str,
    ) -> None:
        if (
            entry.user_id != user_id
            or to_money(entry.amount) != amount
            or entry.kind != kind
        ):
            raise InvalidOperationError(
                "Idempotency key was already used for a different balance movement"
            )

    @staticmethod
    def _validate_existing_run(
        run: BillingRun,
        *,
        period_start: datetime,
        period_end: datetime,
        cost_per_config: Decimal,
    ) -> None:
        # SQLite drops timezone metadata; compare instants after normalizing.
        def naive_utc(value: datetime) -> datetime:
            if value.tzinfo is None:
                return value
            return value.astimezone(timezone.utc).replace(tzinfo=None)

        if (
            naive_utc(run.period_start) != naive_utc(period_start)
            or naive_utc(run.period_end) != naive_utc(period_end)
            or to_money(run.cost_per_config) != cost_per_config
        ):
            raise InvalidOperationError(
                "Billing period key was already used with different parameters"
            )

    @staticmethod
    def _validate_payment(
        payment: ProviderPayment,
        *,
        user_id: int,
        amount: Decimal,
        currency: str,
        payload: str,
        require_payload: bool,
    ) -> None:
        if (
            payment.user_id != user_id
            or to_money(payment.amount) != amount
            or payment.currency != currency
            or (require_payload and payment.payload != payload)
        ):
            raise InvalidOperationError("Payment confirmation does not match intent")

    @staticmethod
    def _ensure_intent_active(payment: ProviderPayment) -> None:
        expires_at = payment.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise InvalidOperationError("Payment intent has expired")

    @staticmethod
    def _positive_amount(value: Decimal | int | float | str) -> Decimal:
        amount = to_money(value)
        if amount <= 0:
            raise InvalidOperationError("Amount must be positive")
        return amount

    @staticmethod
    def _provider(value: str) -> str:
        provider = value.strip().lower()
        if not provider or len(provider) > 32:
            raise InvalidOperationError("Invalid payment provider")
        return provider

    @staticmethod
    def _currency(value: str) -> str:
        currency = value.strip().upper()
        # User balances currently have one implicit denomination. Accepting a
        # second currency without FX and minor-unit metadata would silently mix
        # incompatible money in the same ledger.
        if currency != "RUB":
            raise InvalidOperationError("Only RUB payments are supported")
        return currency
