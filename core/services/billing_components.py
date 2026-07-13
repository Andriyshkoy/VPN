from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid4, uuid5

from core.config import settings
from core.db.models.ledger import LedgerKind
from core.db.repo.billing import to_money
from core.domain import VPNOperationKind, VPNState
from core.exceptions import (
    APIConfigurationError,
    APIRequestRejectedError,
    ConfigNotFoundError,
    InvalidOperationError,
    UserNotFoundError,
)

from .billing_contracts import PaymentIntent, PaymentReceipt
from .models import Config, User


class BalanceOperations:
    """Manual balance mutations and their transactional entitlement intents."""

    async def top_up(
        self,
        user_id: int,
        amount: Decimal | int | float | str,
        *,
        idempotency_key: str | None = None,
    ) -> User:
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


class ProviderPaymentOperations:
    """Provider payment intent validation and idempotent capture."""

    async def create_payment_intent(
        self,
        *,
        user_id: int,
        amount: Decimal | int | float | str,
        provider: str = "telegram",
        currency: str = "RUB",
        idempotency_key: str | None = None,
    ) -> PaymentIntent:
        intent_id = None
        if idempotency_key is not None:
            if (
                not isinstance(idempotency_key, str)
                or not idempotency_key.strip()
                or len(idempotency_key) > 160
            ):
                raise InvalidOperationError("Invalid payment intent idempotency key")
            intent_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"payment-intent:{user_id}:{provider}:{idempotency_key.strip()}",
                )
            )
        async with self._uow() as repos:
            payment = await self._billing_repo(repos).create_payment_intent(
                user_id=user_id,
                provider=provider,
                amount=amount,
                currency=currency,
                intent_id=intent_id,
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
        claim_id: str,
        payload: str,
        amount: Decimal | int | float | str,
        currency: str,
        provider: str = "telegram",
    ) -> PaymentIntent:
        async with self._uow() as repos:
            payment = await self._billing_repo(repos).validate_payment_intent(
                user_id=user_id,
                claim_id=claim_id,
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

    async def claim_payment_invoice_delivery(
        self,
        *,
        user_id: int,
        intent_id: str,
        provider: str = "telegram",
    ) -> bool:
        """Claim the at-most-once provider invoice delivery for an intent."""

        async with self._uow() as repos:
            return await self._billing_repo(repos).claim_invoice_delivery_attempt(
                user_id=user_id,
                intent_id=intent_id,
                provider=provider,
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


class PeriodicBillingOperations:
    """Periodic charges, notification production and lifecycle reconciliation."""

    async def charge_all(
        self, *, period_key: str | None = None, at: datetime | None = None
    ) -> dict[User, Decimal]:
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
            notification_candidates = [] if settings.notifications_enabled else None
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
        lifecycle = await self._config_service.reconcile(limit=limit)
        async with self._uow() as repos:
            refunds = await self._billing_repo(
                repos
            ).refund_rejected_config_reservations()
        return lifecycle, refunds


class PaidProvisioningOperations:
    """Atomic paid provisioning and its exact compensating transaction."""

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
    ) -> Config:
        if settings.maintenance_mode or not settings.provisioning_enabled:
            raise InvalidOperationError(
                "VPN configuration provisioning is temporarily disabled"
            )

        creation_cost = to_money(creation_cost)
        if creation_cost < 0:
            raise InvalidOperationError("Configuration cost cannot be negative")

        display_name = self._config_service._validate_display_name(display_name)

        if idempotency_key is None:
            operation_id = str(uuid4())
            paid_request_payload = None
        else:
            if (
                not isinstance(idempotency_key, str)
                or not idempotency_key.strip()
                or len(idempotency_key) > 160
            ):
                raise InvalidOperationError("Invalid idempotency key")
            normalized_key = idempotency_key.strip()
            operation_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"vpn-paid-config:{owner_id}:{normalized_key}",
                )
            )
            paid_request_payload = {
                "paid_request_version": 1,
                "creation_cost": f"{creation_cost:.2f}",
                "display_name": display_name,
            }

        reservation_key = f"config-reservation:{operation_id}"
        existing_config_id: int | None = None
        context = None
        async with self._uow() as repos:
            if paid_request_payload is not None:
                # Serialize requests for one account before checking the
                # deterministic operation ID. This closes the concurrent
                # replay window without relying on an IntegrityError after a
                # remote side effect has already been committed.
                if await repos["users"].get_for_update(owner_id) is None:
                    raise UserNotFoundError(f"User with ID {owner_id} not found")
                existing_operation = await repos["vpn_operations"].get(
                    operation_id=operation_id
                )
                if existing_operation is not None:
                    existing_config = await self._validate_paid_config_replay(
                        repos,
                        existing_operation,
                        server_id=server_id,
                        owner_id=owner_id,
                        name=name,
                        display_name=display_name,
                        creation_cost=creation_cost,
                        use_password=use_password,
                    )
                    existing_config_id = existing_config.id

            if existing_config_id is None:
                context = await self._config_service.prepare_config(
                    repos=repos,
                    operation_id=operation_id,
                    server_id=server_id,
                    owner_id=owner_id,
                    name=name,
                    display_name=display_name,
                    use_password=use_password,
                    operation_payload=paid_request_payload,
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

        if existing_config_id is not None:
            # A prior attempt committed the financial/config intent but may
            # have crashed before delivering the profile. Nudge the same
            # durable operation and return the same config; never create or
            # charge again.
            await self._config_service.execute_operations(
                (operation_id,), owner_id=owner_id
            )
            existing = await self._config_service.get(existing_config_id)
            if existing is None:
                raise ConfigNotFoundError(
                    "Paid configuration replay references a missing config"
                )
            return existing

        try:
            if context is None:  # pragma: no cover - defensive invariant
                raise RuntimeError("Paid provisioning context was not prepared")
            return await self._config_service.execute_prepared(context)
        except (APIConfigurationError, APIRequestRejectedError):
            if creation_cost > 0:
                async with self._uow() as repos:
                    await self._billing_repo(repos).refund_rejected_config_reservations(
                        operation_id=operation_id
                    )
            raise

    async def _validate_paid_config_replay(
        self,
        repos,
        operation,
        *,
        server_id: int,
        owner_id: int,
        name: str,
        display_name: str,
        creation_cost: Decimal,
        use_password: bool,
    ):
        """Return the first config only when every immutable request field matches."""

        payload = operation.payload if isinstance(operation.payload, dict) else {}
        expected = {
            "paid_request_version": 1,
            "creation_cost": f"{creation_cost:.2f}",
            "display_name": display_name,
            "use_password": bool(use_password),
        }
        identity_matches = (
            operation.kind == VPNOperationKind.PROVISION.value
            and operation.owner_id == owner_id
            and operation.server_id == server_id
            and operation.config_name == name
            and all(payload.get(key) == value for key, value in expected.items())
            and operation.config_id is not None
        )
        if not identity_matches:
            raise InvalidOperationError(
                "Idempotency key was already used for another VPN purchase"
            )

        config = await repos["configs"].get(id=operation.config_id)
        if config is None:
            raise ConfigNotFoundError(
                "Paid configuration replay references a removed config"
            )
        if (
            config.owner_id != owner_id
            or config.server_id != server_id
            or config.name != name
        ):
            raise InvalidOperationError(
                "Idempotent VPN purchase no longer matches its config"
            )
        return config
