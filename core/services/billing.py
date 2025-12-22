from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Callable

from core.exceptions import (
    InsufficientBalanceError,
    ServerNotFoundError,
    UserNotFoundError,
)

from .api_gateway import APIGateway
from .config import ConfigService
from .models import BalanceTransaction, BillingSettings, Config, User

MONEY_QUANT = Decimal("0.01")
MONTH_HOURS = Decimal("720")
MIN_BILLING_INTERVAL = timedelta(hours=1)


class BillingService:
    """Service that handles balance operations and usage billing."""

    def __init__(self, uow: Callable) -> None:
        """
        Initialize the billing service.
        :param uow: Unit of Work factory to manage database transactions.
        """
        self._uow = uow
        self._config_service = ConfigService(uow)

    async def get_settings(self) -> BillingSettings:
        async with self._uow() as repos:
            settings = await repos["billing_settings"].get_or_create()
            return BillingSettings.from_orm(settings)

    async def update_settings(
        self,
        *,
        config_creation_cost: float | None = None,
        monthly_config_cost: float | None = None,
        referral_first_deposit_bonus_pct: float | None = None,
        referral_recurring_bonus_pct: float | None = None,
    ) -> BillingSettings:
        fields: dict[str, object] = {}
        if config_creation_cost is not None:
            fields["config_creation_cost"] = Decimal(str(config_creation_cost))
        if monthly_config_cost is not None:
            fields["monthly_config_cost"] = Decimal(str(monthly_config_cost))
        if referral_first_deposit_bonus_pct is not None:
            fields["referral_first_deposit_bonus_pct"] = Decimal(
                str(referral_first_deposit_bonus_pct)
            )
        if referral_recurring_bonus_pct is not None:
            fields["referral_recurring_bonus_pct"] = Decimal(
                str(referral_recurring_bonus_pct)
            )
        async with self._uow() as repos:
            await repos["billing_settings"].get_or_create()
            if not fields:
                settings = await repos["billing_settings"].get_or_create()
                return BillingSettings.from_orm(settings)
            updated = await repos["billing_settings"].update(**fields)
            return BillingSettings.from_orm(updated)

    async def list_transactions(
        self,
        *,
        user_id: int,
        limit: int | None = None,
        offset: int = 0,
        kinds: list[str] | None = None,
        amount_sign: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[BalanceTransaction]:
        async with self._uow() as repos:
            txs = await repos["transactions"].list_for_user(
                user_id=user_id,
                limit=limit,
                offset=offset,
                kinds=kinds,
                amount_sign=amount_sign,
                start=start,
                end=end,
            )
            return [BalanceTransaction.from_orm(t) for t in txs]

    async def get_referral_bonus_totals(
        self,
        *,
        user_id: int,
        related_user_ids: list[int],
    ) -> dict[int, Decimal]:
        async with self._uow() as repos:
            return await repos["transactions"].sum_referral_bonus_by_related(
                user_id=user_id,
                related_user_ids=related_user_ids,
            )

    async def has_transactions_before(
        self,
        *,
        user_id: int,
        before: datetime,
        kinds: list[str] | None = None,
        amount_sign: str | None = None,
    ) -> bool:
        async with self._uow() as repos:
            return await repos["transactions"].exists_before(
                user_id=user_id,
                before=before,
                kinds=kinds,
                amount_sign=amount_sign,
            )

    async def top_up(
        self,
        user_id: int,
        amount: float,
        *,
        source: str = "admin",
        description: str | None = None,
    ) -> User:
        """Increase user's balance by ``amount`` and return the updated user."""
        delta = Decimal(str(amount)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
        if delta <= 0:
            raise ValueError("Top-up amount must be positive")

        referrer_balance = None
        referrer_id = None
        async with self._uow() as repos:
            user = await repos["users"].apply_balance_delta(user_id, delta)
            if not user:
                raise UserNotFoundError(f"User with ID {user_id} not found")
            await repos["transactions"].create(
                user_id=user_id,
                amount=delta,
                kind="topup",
                source=source,
                description=description,
            )

            if user.referred_by_id:
                referrer = await repos["users"].get(id=user.referred_by_id)
                if referrer:
                    settings = await repos["billing_settings"].get_or_create()
                    is_first = await repos["users"].mark_referral_first_bonus_paid(user.id)
                    bonus_pct = (
                        settings.referral_first_deposit_bonus_pct
                        if is_first
                        else settings.referral_recurring_bonus_pct
                    )
                    if bonus_pct > 0:
                        bonus = (
                            delta * bonus_pct / Decimal("100")
                        ).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
                        if bonus > 0:
                            updated_referrer = await repos["users"].apply_balance_delta(
                                referrer.id, bonus
                            )
                            if not updated_referrer:
                                raise UserNotFoundError(
                                    f"User with ID {referrer.id} not found"
                                )
                            referrer_id = updated_referrer.id
                            referrer_balance = updated_referrer.balance
                            bonus_label = "first_deposit" if is_first else "recurring"
                            await repos["transactions"].create(
                                user_id=updated_referrer.id,
                                amount=bonus,
                                kind="referral_bonus",
                                source="referral",
                                description=(
                                    f"{bonus_label} bonus from user {user.id} via {source}"
                                ),
                                related_user_id=user.id,
                            )

        if user.balance > 0:
            await self._config_service.unsuspend_all(user_id)
        if referrer_id is not None and referrer_balance is not None and referrer_balance > 0:
            await self._config_service.unsuspend_all(referrer_id)

        return User.from_orm(user)

    async def withdraw(
        self,
        user_id: int,
        amount: float,
        *,
        source: str = "admin",
        description: str | None = None,
    ) -> User:
        """Deduct ``amount`` from user's balance and return updated user."""
        delta = Decimal(str(amount)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
        if delta <= 0:
            raise ValueError("Withdraw amount must be positive")

        async with self._uow() as repos:
            user = await repos["users"].reserve_balance(user_id, delta)
            if not user:
                existing = await repos["users"].get(id=user_id)
                if not existing:
                    raise UserNotFoundError(f"User with ID {user_id} not found")
                raise InsufficientBalanceError("Insufficient balance")
            await repos["transactions"].create(
                user_id=user_id,
                amount=-delta,
                kind="withdraw",
                source=source,
                description=description,
            )

        if user.balance <= 0:
            await self._config_service.suspend_all(user_id)

        return User.from_orm(user)

    async def create_paid_config(
        self,
        *,
        server_id: int,
        owner_id: int,
        name: str,
        display_name: str,
        use_password: bool = False,
        source: str = "config_creation",
    ) -> Config:
        """Create config and charge configured creation cost."""
        api_created = False
        server = None

        try:
            async with self._uow() as repos:
                settings = await repos["billing_settings"].get_or_create()
                cost = settings.config_creation_cost

                server = await repos["servers"].get(id=server_id)
                if not server:
                    raise ServerNotFoundError(f"Server {server_id} not found")

                user = await repos["users"].reserve_balance(owner_id, cost)
                if not user:
                    existing = await repos["users"].get(id=owner_id)
                    if not existing:
                        raise UserNotFoundError(f"User with ID {owner_id} not found")
                    raise InsufficientBalanceError("Insufficient balance")

                cfg = await self._config_service.create_config(
                    server_id=server_id,
                    owner_id=owner_id,
                    name=name,
                    display_name=display_name,
                    use_password=use_password,
                    repos=repos,
                )
                api_created = True
                await repos["transactions"].create(
                    user_id=owner_id,
                    amount=-cost,
                    kind="config_creation",
                    source=source,
                    config_id=cfg.id,
                )
                return cfg
        except Exception:
            if api_created and server is not None:
                try:
                    async with APIGateway(server.ip, server.port, server.api_key) as api:
                        await api.revoke_client(name)
                except Exception:
                    pass
            raise

    def _calc_usage_charge(self, monthly_cost: Decimal, hours: int) -> Decimal:
        raw = (monthly_cost * Decimal(hours)) / MONTH_HOURS
        return raw.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)

    async def charge_usage(
        self,
        *,
        now: datetime | None = None,
    ) -> dict[User, Decimal]:
        """Charge users for active configs based on elapsed hours."""
        now = now or datetime.now(timezone.utc).replace(tzinfo=None)
        billable_before = now - MIN_BILLING_INTERVAL

        async with self._uow() as repos:
            settings = await repos["billing_settings"].get_or_create()
            monthly_cost = settings.monthly_config_cost
            if monthly_cost <= 0:
                return {}
            configs = await repos["configs"].list_billable(before=billable_before)

        by_user: dict[int, list[tuple[object, int, Decimal, datetime]]] = defaultdict(list)
        for cfg in configs:
            elapsed = now - cfg.last_billed_at
            hours = int(elapsed.total_seconds() // 3600)
            if hours <= 0:
                continue
            charge = self._calc_usage_charge(monthly_cost, hours)
            if charge <= 0:
                continue
            new_last_billed_at = cfg.last_billed_at + timedelta(hours=hours)
            by_user[cfg.owner_id].append((cfg, hours, charge, new_last_billed_at))

        charged: dict[User, Decimal] = {}
        for owner_id, entries in by_user.items():
            total_charge = Decimal("0.00")
            updated_user = None
            async with self._uow() as repos:
                for cfg, hours, charge, new_last_billed_at in entries:
                    updated = await repos["configs"].advance_billing(
                        cfg.id, cfg.last_billed_at, new_last_billed_at
                    )
                    if not updated:
                        continue
                    total_charge += charge
                    await repos["transactions"].create(
                        user_id=owner_id,
                        amount=-charge,
                        kind="usage",
                        source="billing",
                        description=f"{hours}h usage",
                        config_id=cfg.id,
                    )

                if total_charge > 0:
                    updated_user = await repos["users"].apply_balance_delta(
                        owner_id, -total_charge
                    )
                    if not updated_user:
                        raise UserNotFoundError(
                            f"User with ID {owner_id} not found"
                        )

            if updated_user:
                user_dc = User.from_orm(updated_user)
                charged[user_dc] = total_charge
                if updated_user.balance <= 0:
                    await self._config_service.suspend_all(owner_id)
        return charged
