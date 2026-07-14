from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import aliased

from core.db.models.billing_run import BillingRun
from core.db.models.config import VPN_Config
from core.db.models.ledger import LedgerEntry, LedgerKind
from core.db.models.payment import ProviderPayment
from core.db.models.referral_reward import ReferralReward
from core.db.models.server import Server
from core.db.models.user import User
from core.db.models.vpn_operation import VPNOperation

MONEY_QUANTUM = Decimal("0.01")
POSTGRES_INTEGER_MAX = 2_147_483_647
POSTGRES_BIGINT_MAX = 9_223_372_036_854_775_807


def numeric_search_predicates(
    value: str | None,
    *,
    integer_columns: tuple[Any, ...] = (),
    bigint_columns: tuple[Any, ...] = (),
) -> tuple[Any, ...]:
    """Build decimal search predicates without overflowing PostgreSQL binds.

    SQLAlchemy/asyncpg bind a comparison according to the column type.  A value
    which fits ``BIGINT`` can therefore still fail before query execution when
    it is reused for an ``INTEGER`` primary-key comparison.  Keep the bounds
    per column family instead of applying one broad numeric guard.
    """

    normalized = (value or "").strip()
    if not normalized or not normalized.isascii() or not normalized.isdecimal():
        return ()
    number = int(normalized)
    if number > POSTGRES_BIGINT_MAX:
        return ()

    predicates = []
    if number <= POSTGRES_INTEGER_MAX:
        predicates.extend(column == number for column in integer_columns)
    predicates.extend(column == number for column in bigint_columns)
    return tuple(predicates)


def money(value: Decimal | int | float | str | None) -> str:
    """Return an exact, frontend-safe decimal string."""

    amount = Decimal(str(value or 0)).quantize(MONEY_QUANTUM, ROUND_HALF_UP)
    return format(amount, ".2f")


def utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def naive_utc(value: datetime) -> datetime:
    """Bind a UTC instant to legacy timestamp-without-time-zone columns."""

    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _decimal(value: Decimal | int | float | str | None) -> Decimal:
    return Decimal(str(value or 0)).quantize(MONEY_QUANTUM, ROUND_HALF_UP)


class AdminUserQueryService:
    """Read models used by the versioned administrator API."""

    MAX_PAGE_SIZE = 100

    def __init__(self, uow: Callable):
        self._uow = uow

    async def list_users(
        self,
        *,
        q: str | None = None,
        delivery_status: str | None = None,
        has_configs: bool | None = None,
        config_state: str | None = None,
        has_payments: bool | None = None,
        referrer_id: int | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        sort: str = "created_at",
        order: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit = min(max(int(limit), 1), self.MAX_PAGE_SIZE)
        offset = max(int(offset), 0)

        config_stats = (
            select(
                VPN_Config.owner_id.label("owner_id"),
                func.count(VPN_Config.id).label("config_total"),
                func.sum(case((VPN_Config.actual_state == "active", 1), else_=0)).label(
                    "config_active"
                ),
                func.sum(
                    case((VPN_Config.actual_state == "suspended", 1), else_=0)
                ).label("config_suspended"),
                func.sum(
                    case(
                        (
                            VPN_Config.desired_state != VPN_Config.actual_state,
                            1,
                        ),
                        else_=0,
                    )
                ).label("config_pending"),
                func.sum(case((VPN_Config.actual_state == "failed", 1), else_=0)).label(
                    "config_failed"
                ),
            )
            .group_by(VPN_Config.owner_id)
            .subquery()
        )
        payment_stats = (
            select(
                ProviderPayment.user_id.label("user_id"),
                func.sum(
                    case(
                        (ProviderPayment.status == "credited", ProviderPayment.amount),
                        else_=Decimal("0.00"),
                    )
                ).label("credited_total"),
                func.max(
                    case(
                        (
                            ProviderPayment.status == "credited",
                            ProviderPayment.credited_at,
                        )
                    )
                ).label("last_payment_at"),
                func.sum(
                    case((ProviderPayment.status == "credited", 1), else_=0)
                ).label("credited_count"),
            )
            .group_by(ProviderPayment.user_id)
            .subquery()
        )
        referral_stats = (
            select(
                User.referred_by_id.label("referrer_id"),
                func.count(User.id).label("direct_referrals"),
            )
            .where(User.referred_by_id.is_not(None))
            .group_by(User.referred_by_id)
            .subquery()
        )
        referrer = aliased(User)

        base = (
            select(
                User,
                referrer.username.label("referrer_username"),
                func.coalesce(config_stats.c.config_total, 0).label("config_total"),
                func.coalesce(config_stats.c.config_active, 0).label("config_active"),
                func.coalesce(config_stats.c.config_suspended, 0).label(
                    "config_suspended"
                ),
                func.coalesce(config_stats.c.config_pending, 0).label("config_pending"),
                func.coalesce(config_stats.c.config_failed, 0).label("config_failed"),
                func.coalesce(payment_stats.c.credited_total, 0).label(
                    "credited_total"
                ),
                payment_stats.c.last_payment_at,
                func.coalesce(referral_stats.c.direct_referrals, 0).label(
                    "direct_referrals"
                ),
            )
            .outerjoin(referrer, referrer.id == User.referred_by_id)
            .outerjoin(config_stats, config_stats.c.owner_id == User.id)
            .outerjoin(payment_stats, payment_stats.c.user_id == User.id)
            .outerjoin(referral_stats, referral_stats.c.referrer_id == User.id)
        )

        conditions = []
        normalized_q = (q or "").strip()
        if normalized_q:
            escaped = (
                normalized_q.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            config_match = (
                select(VPN_Config.id)
                .where(
                    VPN_Config.owner_id == User.id,
                    or_(
                        VPN_Config.name.ilike(f"%{escaped}%", escape="\\"),
                        VPN_Config.display_name.ilike(f"%{escaped}%", escape="\\"),
                    ),
                )
                .exists()
            )
            terms = [
                User.username.ilike(f"%{escaped}%", escape="\\"),
                config_match,
            ]
            terms.extend(
                numeric_search_predicates(
                    normalized_q,
                    integer_columns=(User.id,),
                    bigint_columns=(User.tg_id,),
                )
            )
            conditions.append(or_(*terms))
        if delivery_status:
            conditions.append(User.telegram_delivery_status == delivery_status)
        if referrer_id is not None:
            conditions.append(User.referred_by_id == referrer_id)
        if created_from is not None:
            conditions.append(User.created >= naive_utc(created_from))
        if created_to is not None:
            conditions.append(User.created < naive_utc(created_to))
        if has_configs is True:
            conditions.append(func.coalesce(config_stats.c.config_total, 0) > 0)
        elif has_configs is False:
            conditions.append(func.coalesce(config_stats.c.config_total, 0) == 0)
        state_columns = {
            "active": config_stats.c.config_active,
            "suspended": config_stats.c.config_suspended,
            "pending": config_stats.c.config_pending,
            "failed": config_stats.c.config_failed,
        }
        if config_state in state_columns:
            conditions.append(func.coalesce(state_columns[config_state], 0) > 0)
        if has_payments is True:
            conditions.append(func.coalesce(payment_stats.c.credited_count, 0) > 0)
        elif has_payments is False:
            conditions.append(func.coalesce(payment_stats.c.credited_count, 0) == 0)

        if conditions:
            base = base.where(*conditions)

        sort_columns = {
            "created_at": User.created,
            "balance": User.balance,
            "last_payment_at": payment_stats.c.last_payment_at,
            "username": User.username,
        }
        sort_column = sort_columns.get(sort, User.created)
        ordering = sort_column.asc() if order == "asc" else sort_column.desc()

        count_stmt = select(func.count()).select_from(base.order_by(None).subquery())
        page_stmt = base.order_by(ordering, User.id.desc()).offset(offset).limit(limit)

        async with self._uow() as repos:
            session = repos["users"].session
            total = int(await session.scalar(count_stmt) or 0)
            rows = (await session.execute(page_stmt)).all()

        return {
            "items": [self._user_list_item(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @staticmethod
    def _user_list_item(row: Any) -> dict[str, Any]:
        user = row[0]
        return {
            "id": user.id,
            "tg_id": user.tg_id,
            "username": user.username,
            "created_at": utc_iso(user.created),
            "balance": money(user.balance),
            "delivery_status": user.telegram_delivery_status,
            "blocked_at": utc_iso(user.telegram_blocked_at),
            "referrer": (
                {
                    "id": user.referred_by_id,
                    "username": row.referrer_username,
                }
                if user.referred_by_id is not None
                else None
            ),
            "config_counts": {
                "total": int(row.config_total or 0),
                "active": int(row.config_active or 0),
                "suspended": int(row.config_suspended or 0),
                "pending": int(row.config_pending or 0),
                "failed": int(row.config_failed or 0),
            },
            "credited_total": money(row.credited_total),
            "last_payment_at": utc_iso(row.last_payment_at),
            "direct_referrals": int(row.direct_referrals or 0),
        }

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        direct = aliased(User)
        second = aliased(User)
        async with self._uow() as repos:
            session = repos["users"].session
            user = await session.get(User, user_id)
            if user is None:
                return None
            referrer = (
                await session.get(User, user.referred_by_id)
                if user.referred_by_id is not None
                else None
            )

            latest_ledger_id = int(
                await session.scalar(
                    select(func.max(LedgerEntry.id)).where(
                        LedgerEntry.user_id == user_id
                    )
                )
                or 0
            )
            ledger_totals = (
                await session.execute(
                    select(
                        func.coalesce(
                            func.sum(
                                case(
                                    (
                                        LedgerEntry.kind
                                        == LedgerKind.PERIODIC_CHARGE.value,
                                        -LedgerEntry.amount,
                                    ),
                                    else_=Decimal("0.00"),
                                )
                            ),
                            Decimal("0.00"),
                        ).label("service_charges"),
                        func.coalesce(
                            func.sum(
                                case(
                                    (
                                        LedgerEntry.kind
                                        == LedgerKind.CONFIG_RESERVATION.value,
                                        -LedgerEntry.amount,
                                    ),
                                    else_=Decimal("0.00"),
                                )
                            ),
                            Decimal("0.00"),
                        ).label("config_fees"),
                        func.coalesce(
                            func.sum(
                                case(
                                    (
                                        LedgerEntry.kind
                                        == LedgerKind.CONFIG_REFUND.value,
                                        LedgerEntry.amount,
                                    ),
                                    else_=Decimal("0.00"),
                                )
                            ),
                            Decimal("0.00"),
                        ).label("config_refunds"),
                        func.coalesce(
                            func.sum(
                                case(
                                    (
                                        LedgerEntry.kind.in_(
                                            (
                                                LedgerKind.REFERRAL_REWARD_L1.value,
                                                LedgerKind.REFERRAL_REWARD_L2.value,
                                            )
                                        ),
                                        LedgerEntry.amount,
                                    ),
                                    else_=Decimal("0.00"),
                                )
                            ),
                            Decimal("0.00"),
                        ).label("referral_rewards"),
                        func.coalesce(
                            func.sum(
                                case(
                                    (
                                        LedgerEntry.kind.in_(
                                            (
                                                LedgerKind.MANUAL_TOP_UP.value,
                                                LedgerKind.MANUAL_WITHDRAWAL.value,
                                                LedgerKind.ADMIN_ADJUSTMENT.value,
                                            )
                                        ),
                                        LedgerEntry.amount,
                                    ),
                                    else_=Decimal("0.00"),
                                )
                            ),
                            Decimal("0.00"),
                        ).label("manual_adjustments"),
                    ).where(LedgerEntry.user_id == user_id)
                )
            ).one()
            payment_totals = (
                await session.execute(
                    select(
                        func.coalesce(func.sum(ProviderPayment.amount), 0),
                        func.max(ProviderPayment.credited_at),
                    ).where(
                        ProviderPayment.user_id == user_id,
                        ProviderPayment.status == "credited",
                    )
                )
            ).one()
            config_counts = (
                await session.execute(
                    select(
                        func.count(VPN_Config.id),
                        func.sum(
                            case((VPN_Config.actual_state == "active", 1), else_=0)
                        ),
                        func.sum(
                            case((VPN_Config.actual_state == "suspended", 1), else_=0)
                        ),
                        func.sum(
                            case(
                                (
                                    VPN_Config.desired_state != VPN_Config.actual_state,
                                    1,
                                ),
                                else_=0,
                            )
                        ),
                        func.sum(
                            case((VPN_Config.actual_state == "failed", 1), else_=0)
                        ),
                    ).where(VPN_Config.owner_id == user_id)
                )
            ).one()
            level_1_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(direct)
                    .where(direct.referred_by_id == user_id)
                )
                or 0
            )
            level_2_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(second)
                    .join(direct, second.referred_by_id == direct.id)
                    .where(direct.referred_by_id == user_id)
                )
                or 0
            )
            referral_earned = (
                await session.execute(
                    select(
                        func.coalesce(
                            func.sum(
                                case(
                                    (
                                        ReferralReward.level == 1,
                                        ReferralReward.reward_amount,
                                    ),
                                    else_=Decimal("0.00"),
                                )
                            ),
                            0,
                        ),
                        func.coalesce(
                            func.sum(
                                case(
                                    (
                                        ReferralReward.level == 2,
                                        ReferralReward.reward_amount,
                                    ),
                                    else_=Decimal("0.00"),
                                )
                            ),
                            0,
                        ),
                    ).where(ReferralReward.beneficiary_user_id == user_id)
                )
            ).one()

        return {
            "identity": {
                "id": user.id,
                "tg_id": user.tg_id,
                "username": user.username,
                "created_at": utc_iso(user.created),
                "delivery_status": user.telegram_delivery_status,
                "blocked_at": utc_iso(user.telegram_blocked_at),
                "last_delivery_error": user.telegram_last_delivery_error,
                "referral_code": user.referral_code,
            },
            "finance": {
                "balance": money(user.balance),
                "latest_ledger_entry_id": latest_ledger_id,
                "provider_deposits": money(payment_totals[0]),
                "service_charges": money(ledger_totals.service_charges),
                "config_fees": money(ledger_totals.config_fees),
                "config_refunds": money(ledger_totals.config_refunds),
                "referral_rewards": money(ledger_totals.referral_rewards),
                "manual_adjustments": money(ledger_totals.manual_adjustments),
                "last_payment_at": utc_iso(payment_totals[1]),
            },
            "configs": {
                "total": int(config_counts[0] or 0),
                "active": int(config_counts[1] or 0),
                "suspended": int(config_counts[2] or 0),
                "pending": int(config_counts[3] or 0),
                "failed": int(config_counts[4] or 0),
            },
            "referral": {
                "referrer": (
                    {
                        "id": referrer.id,
                        "tg_id": referrer.tg_id,
                        "username": referrer.username,
                    }
                    if referrer is not None
                    else None
                ),
                "level_1_count": level_1_count,
                "level_2_count": level_2_count,
                "level_1_earned": money(referral_earned[0]),
                "level_2_earned": money(referral_earned[1]),
                "total_earned": money(
                    _decimal(referral_earned[0]) + _decimal(referral_earned[1])
                ),
            },
        }

    async def list_ledger(
        self,
        user_id: int,
        *,
        direction: str | None = None,
        kind: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        snapshot_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any] | None:
        limit = min(max(limit, 1), self.MAX_PAGE_SIZE)
        offset = max(offset, 0)
        async with self._uow() as repos:
            session = repos["users"].session
            if await session.get(User, user_id) is None:
                return None
            if snapshot_id is None:
                snapshot_id = int(
                    await session.scalar(
                        select(func.max(LedgerEntry.id)).where(
                            LedgerEntry.user_id == user_id
                        )
                    )
                    or 0
                )
            conditions = [
                LedgerEntry.user_id == user_id,
                LedgerEntry.id <= snapshot_id,
            ]
            if direction == "credit":
                conditions.append(LedgerEntry.amount > 0)
            elif direction == "debit":
                conditions.append(LedgerEntry.amount < 0)
            if kind:
                conditions.append(LedgerEntry.kind == kind)
            if created_from:
                conditions.append(LedgerEntry.created_at >= created_from)
            if created_to:
                conditions.append(LedgerEntry.created_at < created_to)
            total = int(
                await session.scalar(
                    select(func.count()).select_from(LedgerEntry).where(*conditions)
                )
                or 0
            )
            rows = (
                await session.scalars(
                    select(LedgerEntry)
                    .where(*conditions)
                    .order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
        return {
            "items": [
                {
                    "id": row.id,
                    "amount": money(row.amount),
                    "balance_after": money(row.balance_after),
                    "kind": row.kind,
                    "reference_type": row.reference_type,
                    "reference_id": row.reference_id,
                    "details": dict(row.details or {}),
                    "created_at": utc_iso(row.created_at),
                }
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
            "snapshot_id": snapshot_id,
        }

    async def list_payments(
        self,
        user_id: int,
        *,
        status: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any] | None:
        limit = min(max(limit, 1), self.MAX_PAGE_SIZE)
        offset = max(offset, 0)
        async with self._uow() as repos:
            session = repos["users"].session
            if await session.get(User, user_id) is None:
                return None
            conditions = [ProviderPayment.user_id == user_id]
            if status:
                conditions.append(ProviderPayment.status == status)
            if created_from:
                conditions.append(ProviderPayment.created_at >= created_from)
            if created_to:
                conditions.append(ProviderPayment.created_at < created_to)
            total = int(
                await session.scalar(
                    select(func.count()).select_from(ProviderPayment).where(*conditions)
                )
                or 0
            )
            rows = (
                await session.scalars(
                    select(ProviderPayment)
                    .where(*conditions)
                    .order_by(
                        ProviderPayment.created_at.desc(), ProviderPayment.id.desc()
                    )
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
        return {
            "items": [
                {
                    "id": row.id,
                    "intent_id": row.intent_id,
                    "provider": row.provider,
                    "amount": money(row.amount),
                    "currency": row.currency,
                    "status": row.status,
                    "created_at": utc_iso(row.created_at),
                    "expires_at": utc_iso(row.expires_at),
                    "credited_at": utc_iso(row.credited_at),
                    "referral_settlement_status": row.referral_settlement_status,
                    "referral_settled_at": utc_iso(row.referral_settled_at),
                }
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    async def list_configs(
        self,
        user_id: int,
        *,
        state: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any] | None:
        limit = min(max(limit, 1), self.MAX_PAGE_SIZE)
        offset = max(offset, 0)
        async with self._uow() as repos:
            session = repos["users"].session
            if await session.get(User, user_id) is None:
                return None
            conditions = [VPN_Config.owner_id == user_id]
            if state:
                conditions.append(VPN_Config.actual_state == state)
            total = int(
                await session.scalar(
                    select(func.count()).select_from(VPN_Config).where(*conditions)
                )
                or 0
            )
            rows = (
                await session.execute(
                    select(VPN_Config, Server.name.label("server_name"))
                    .join(Server, Server.id == VPN_Config.server_id)
                    .where(*conditions)
                    .order_by(VPN_Config.created_at.desc(), VPN_Config.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
        return {
            "items": [
                {
                    "id": row.VPN_Config.id,
                    "name": row.VPN_Config.name,
                    "display_name": row.VPN_Config.display_name,
                    "server_id": row.VPN_Config.server_id,
                    "server_name": row.server_name,
                    "created_at": utc_iso(row.VPN_Config.created_at),
                    "suspended": bool(row.VPN_Config.suspended),
                    "suspended_at": utc_iso(row.VPN_Config.suspended_at),
                    "desired_state": row.VPN_Config.desired_state,
                    "actual_state": row.VPN_Config.actual_state,
                    "operation_id": row.VPN_Config.operation_id,
                    "last_error": row.VPN_Config.last_error,
                    "updated_at": utc_iso(row.VPN_Config.updated_at),
                }
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    async def list_vpn_operations(
        self,
        user_id: int,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any] | None:
        limit = min(max(limit, 1), self.MAX_PAGE_SIZE)
        offset = max(offset, 0)
        async with self._uow() as repos:
            session = repos["users"].session
            if await session.get(User, user_id) is None:
                return None
            conditions = [VPNOperation.owner_id == user_id]
            if status:
                conditions.append(VPNOperation.status == status)
            total = int(
                await session.scalar(
                    select(func.count()).select_from(VPNOperation).where(*conditions)
                )
                or 0
            )
            rows = (
                await session.scalars(
                    select(VPNOperation)
                    .where(*conditions)
                    .order_by(VPNOperation.created_at.desc(), VPNOperation.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
        return {
            "items": [
                {
                    "id": row.id,
                    "operation_id": row.operation_id,
                    "config_id": row.config_id,
                    "config_name": row.config_name,
                    "server_id": row.server_id,
                    "kind": row.kind,
                    "status": row.status,
                    "attempts": row.attempts,
                    "last_error": row.last_error,
                    "created_at": utc_iso(row.created_at),
                    "updated_at": utc_iso(row.updated_at),
                    "completed_at": utc_iso(row.completed_at),
                }
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }


class AdminReferralQueryService:
    MAX_PAGE_SIZE = 100

    def __init__(self, uow: Callable):
        self._uow = uow

    async def ancestry(
        self, user_id: int, *, max_depth: int = 50
    ) -> list[dict[str, Any]] | None:
        max_depth = min(max(max_depth, 1), 50)
        async with self._uow() as repos:
            session = repos["users"].session
            current = await session.get(User, user_id)
            if current is None:
                return None
            result: list[dict[str, Any]] = []
            visited = {current.id}
            depth = 0
            while current.referred_by_id is not None and depth < max_depth:
                parent = await session.get(User, current.referred_by_id)
                if parent is None:
                    break
                if parent.id in visited:
                    result.append(
                        {
                            "id": parent.id,
                            "tg_id": parent.tg_id,
                            "username": parent.username,
                            "depth": depth + 1,
                            "cycle": True,
                        }
                    )
                    break
                visited.add(parent.id)
                depth += 1
                result.append(
                    {
                        "id": parent.id,
                        "tg_id": parent.tg_id,
                        "username": parent.username,
                        "created_at": utc_iso(parent.created),
                        "depth": depth,
                        "cycle": False,
                    }
                )
                current = parent
        return result

    async def children(
        self,
        user_id: int,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any] | None:
        limit = min(max(limit, 1), self.MAX_PAGE_SIZE)
        offset = max(offset, 0)
        child = aliased(User)
        grandchild = aliased(User)
        direct_counts = (
            select(
                grandchild.referred_by_id.label("parent_id"),
                func.count(grandchild.id).label("child_count"),
            )
            .where(grandchild.referred_by_id.is_not(None))
            .group_by(grandchild.referred_by_id)
            .subquery()
        )
        deposits = (
            select(
                ProviderPayment.user_id.label("user_id"),
                func.sum(ProviderPayment.amount).label("deposits"),
            )
            .where(ProviderPayment.status == "credited")
            .group_by(ProviderPayment.user_id)
            .subquery()
        )
        rewards = (
            select(
                ReferralReward.source_user_id.label("source_user_id"),
                func.sum(ReferralReward.reward_amount).label("reward_amount"),
            )
            .where(ReferralReward.beneficiary_user_id == user_id)
            .group_by(ReferralReward.source_user_id)
            .subquery()
        )
        async with self._uow() as repos:
            session = repos["users"].session
            if await session.get(User, user_id) is None:
                return None
            total = int(
                await session.scalar(
                    select(func.count())
                    .select_from(child)
                    .where(child.referred_by_id == user_id)
                )
                or 0
            )
            rows = (
                await session.execute(
                    select(
                        child,
                        func.coalesce(direct_counts.c.child_count, 0).label(
                            "child_count"
                        ),
                        func.coalesce(deposits.c.deposits, 0).label("deposits"),
                        func.coalesce(rewards.c.reward_amount, 0).label(
                            "reward_amount"
                        ),
                    )
                    .outerjoin(direct_counts, direct_counts.c.parent_id == child.id)
                    .outerjoin(deposits, deposits.c.user_id == child.id)
                    .outerjoin(rewards, rewards.c.source_user_id == child.id)
                    .where(child.referred_by_id == user_id)
                    .order_by(child.created.desc(), child.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
        return {
            "items": [
                {
                    "id": row[0].id,
                    "tg_id": row[0].tg_id,
                    "username": row[0].username,
                    "created_at": utc_iso(row[0].created),
                    "referred_by_id": row[0].referred_by_id,
                    "direct_children": int(row.child_count or 0),
                    "provider_deposits": money(row.deposits),
                    "reward_generated": money(row.reward_amount),
                    "delivery_status": row[0].telegram_delivery_status,
                }
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    async def rewards(
        self,
        user_id: int,
        *,
        level: int | None = None,
        source_user_id: int | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any] | None:
        limit = min(max(limit, 1), self.MAX_PAGE_SIZE)
        offset = max(offset, 0)
        source_user = aliased(User)
        conditions = [ReferralReward.beneficiary_user_id == user_id]
        if level is not None:
            conditions.append(ReferralReward.level == level)
        if source_user_id is not None:
            conditions.append(ReferralReward.source_user_id == source_user_id)
        if created_from:
            conditions.append(ReferralReward.created_at >= created_from)
        if created_to:
            conditions.append(ReferralReward.created_at < created_to)
        async with self._uow() as repos:
            session = repos["users"].session
            if await session.get(User, user_id) is None:
                return None
            total = int(
                await session.scalar(
                    select(func.count()).select_from(ReferralReward).where(*conditions)
                )
                or 0
            )
            rows = (
                await session.execute(
                    select(
                        ReferralReward, source_user.username.label("source_username")
                    )
                    .join(source_user, source_user.id == ReferralReward.source_user_id)
                    .where(*conditions)
                    .order_by(
                        ReferralReward.created_at.desc(), ReferralReward.id.desc()
                    )
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
        return {
            "items": [
                {
                    "id": row.ReferralReward.id,
                    "source_payment_id": row.ReferralReward.source_payment_id,
                    "source_user": {
                        "id": row.ReferralReward.source_user_id,
                        "username": row.source_username,
                    },
                    "level": row.ReferralReward.level,
                    "rate_bps": row.ReferralReward.rate_bps,
                    "source_amount": money(row.ReferralReward.source_amount),
                    "reward_amount": money(row.ReferralReward.reward_amount),
                    "currency": row.ReferralReward.currency,
                    "program_version": row.ReferralReward.program_version,
                    "created_at": utc_iso(row.ReferralReward.created_at),
                }
                for row in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }


class AdminAnalyticsQueryService:
    """Business analytics derived from accounting source-of-truth tables."""

    def __init__(self, uow: Callable):
        self._uow = uow

    @staticmethod
    def _validate_timezone(name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Unknown timezone") from exc

    async def overview(
        self, *, period_from: datetime, period_to: datetime
    ) -> dict[str, Any]:
        if period_to <= period_from:
            raise ValueError("Invalid analytics period")
        # `user.created` predates the timezone-aware schema and remains a
        # PostgreSQL `TIMESTAMP WITHOUT TIME ZONE`. Keep API periods aware,
        # but bind naive UTC values only for comparisons to that column.
        user_period_from = naive_utc(period_from)
        user_period_to = naive_utc(period_to)
        async with self._uow() as repos:
            session = repos["users"].session
            total_users = int(
                await session.scalar(select(func.count()).select_from(User)) or 0
            )
            new_users = int(
                await session.scalar(
                    select(func.count())
                    .select_from(User)
                    .where(
                        User.created >= user_period_from,
                        User.created < user_period_to,
                    )
                )
                or 0
            )
            delivery_rows = (
                await session.execute(
                    select(User.telegram_delivery_status, func.count(User.id)).group_by(
                        User.telegram_delivery_status
                    )
                )
            ).all()
            config_rows = (
                await session.execute(
                    select(VPN_Config.actual_state, func.count(VPN_Config.id)).group_by(
                        VPN_Config.actual_state
                    )
                )
            ).all()
            users_with_configs = int(
                await session.scalar(
                    select(func.count(func.distinct(VPN_Config.owner_id)))
                )
                or 0
            )
            paying_users = int(
                await session.scalar(
                    select(func.count(func.distinct(ProviderPayment.user_id))).where(
                        ProviderPayment.status == "credited",
                        ProviderPayment.credited_at >= period_from,
                        ProviderPayment.credited_at < period_to,
                    )
                )
                or 0
            )
            cash_in = _decimal(
                await session.scalar(
                    select(func.coalesce(func.sum(ProviderPayment.amount), 0)).where(
                        ProviderPayment.status == "credited",
                        ProviderPayment.credited_at >= period_from,
                        ProviderPayment.credited_at < period_to,
                    )
                )
            )
            ledger_rows = (
                await session.execute(
                    select(LedgerEntry.kind, func.sum(LedgerEntry.amount))
                    .where(
                        LedgerEntry.created_at >= period_from,
                        LedgerEntry.created_at < period_to,
                    )
                    .group_by(LedgerEntry.kind)
                )
            ).all()
            ledger = {kind: _decimal(amount) for kind, amount in ledger_rows}
            service_charges = -ledger.get(
                LedgerKind.PERIODIC_CHARGE.value, Decimal("0.00")
            )
            config_fees = -ledger.get(
                LedgerKind.CONFIG_RESERVATION.value, Decimal("0.00")
            )
            config_refunds = ledger.get(LedgerKind.CONFIG_REFUND.value, Decimal("0.00"))
            referral_rewards = ledger.get(
                LedgerKind.REFERRAL_REWARD_L1.value, Decimal("0.00")
            ) + ledger.get(LedgerKind.REFERRAL_REWARD_L2.value, Decimal("0.00"))
            manual_adjustments = sum(
                (
                    ledger.get(LedgerKind.MANUAL_TOP_UP.value, Decimal("0.00")),
                    ledger.get(LedgerKind.MANUAL_WITHDRAWAL.value, Decimal("0.00")),
                    ledger.get(LedgerKind.ADMIN_ADJUSTMENT.value, Decimal("0.00")),
                ),
                Decimal("0.00"),
            )
            opening_balances = ledger.get(
                LedgerKind.OPENING_BALANCE.value, Decimal("0.00")
            )
            wallet_liability = _decimal(
                await session.scalar(
                    select(
                        func.coalesce(
                            func.sum(case((User.balance > 0, User.balance), else_=0)),
                            0,
                        )
                    )
                )
            )
            wallet_debt = -_decimal(
                await session.scalar(
                    select(
                        func.coalesce(
                            func.sum(case((User.balance < 0, User.balance), else_=0)),
                            0,
                        )
                    )
                )
            )
            infrastructure_run_rate = _decimal(
                await session.scalar(
                    select(func.coalesce(func.sum(Server.monthly_cost), 0))
                )
            )
            billing_last = await session.scalar(
                select(BillingRun)
                .order_by(BillingRun.period_end.desc(), BillingRun.id.desc())
                .limit(1)
            )
            referral_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(User)
                    .where(
                        User.referred_by_id.is_not(None),
                        User.created >= user_period_from,
                        User.created < user_period_to,
                    )
                )
                or 0
            )

        recognized_revenue = service_charges + config_fees - config_refunds
        period_days = Decimal(
            str(max((period_to - period_from).total_seconds(), 0) / 86400)
        )
        allocated_infra = (
            infrastructure_run_rate * period_days / Decimal("30")
        ).quantize(MONEY_QUANTUM, ROUND_HALF_UP)
        estimated_margin = recognized_revenue - referral_rewards - allocated_infra
        return {
            "period": {
                "from": utc_iso(period_from),
                "to": utc_iso(period_to),
            },
            "users": {
                "total": total_users,
                "new": new_users,
                "with_configs": users_with_configs,
                "paying": paying_users,
                "invited": referral_count,
                "delivery": {str(key): int(value) for key, value in delivery_rows},
            },
            "configs": {str(key): int(value) for key, value in config_rows},
            "finance": {
                "cash_in": money(cash_in),
                "service_charges": money(service_charges),
                "config_fees": money(config_fees),
                "config_refunds": money(config_refunds),
                "recognized_revenue": money(recognized_revenue),
                "referral_rewards": money(referral_rewards),
                "manual_adjustments": money(manual_adjustments),
                "opening_balances": money(opening_balances),
                "wallet_liability": money(wallet_liability),
                "wallet_debt": money(wallet_debt),
                "infrastructure_monthly_run_rate": money(infrastructure_run_rate),
                "allocated_infrastructure_cost": money(allocated_infra),
                "estimated_margin": money(estimated_margin),
            },
            "billing": (
                {
                    "period_key": billing_last.period_key,
                    "status": billing_last.status,
                    "period_start": utc_iso(billing_last.period_start),
                    "period_end": utc_iso(billing_last.period_end),
                    "charged_users": billing_last.charged_users,
                    "total_amount": money(billing_last.total_amount),
                    "completed_at": utc_iso(billing_last.completed_at),
                }
                if billing_last is not None
                else None
            ),
        }

    async def finance_timeseries(
        self,
        *,
        period_from: datetime,
        period_to: datetime,
        granularity: str = "day",
        timezone_name: str = "UTC",
    ) -> list[dict[str, Any]]:
        if period_to <= period_from:
            raise ValueError("Invalid analytics period")
        if granularity not in {"day", "week", "month"}:
            raise ValueError("Invalid granularity")
        tz = self._validate_timezone(timezone_name)
        async with self._uow() as repos:
            session = repos["users"].session
            payments = (
                await session.execute(
                    select(ProviderPayment.credited_at, ProviderPayment.amount).where(
                        ProviderPayment.status == "credited",
                        ProviderPayment.credited_at >= period_from,
                        ProviderPayment.credited_at < period_to,
                    )
                )
            ).all()
            entries = (
                await session.execute(
                    select(
                        LedgerEntry.created_at, LedgerEntry.kind, LedgerEntry.amount
                    ).where(
                        LedgerEntry.created_at >= period_from,
                        LedgerEntry.created_at < period_to,
                    )
                )
            ).all()

        buckets: dict[date, dict[str, Decimal]] = defaultdict(
            lambda: defaultdict(lambda: Decimal("0.00"))
        )
        for credited_at, amount in payments:
            if credited_at is not None:
                buckets[self._bucket(credited_at, granularity, tz)][
                    "cash_in"
                ] += _decimal(amount)
        for created_at, kind, amount in entries:
            key = self._bucket(created_at, granularity, tz)
            value = _decimal(amount)
            if kind == LedgerKind.PERIODIC_CHARGE.value:
                buckets[key]["service_charges"] -= value
            elif kind == LedgerKind.CONFIG_RESERVATION.value:
                buckets[key]["config_fees"] -= value
            elif kind == LedgerKind.CONFIG_REFUND.value:
                buckets[key]["config_refunds"] += value
            elif kind in {
                LedgerKind.REFERRAL_REWARD_L1.value,
                LedgerKind.REFERRAL_REWARD_L2.value,
            }:
                buckets[key]["referral_rewards"] += value
            elif kind in {
                LedgerKind.MANUAL_TOP_UP.value,
                LedgerKind.MANUAL_WITHDRAWAL.value,
                LedgerKind.ADMIN_ADJUSTMENT.value,
            }:
                buckets[key]["manual_adjustments"] += value

        result = []
        for bucket_date in sorted(buckets):
            item = buckets[bucket_date]
            recognized = (
                item["service_charges"] + item["config_fees"] - item["config_refunds"]
            )
            result.append(
                {
                    "bucket": bucket_date.isoformat(),
                    "cash_in": money(item["cash_in"]),
                    "service_charges": money(item["service_charges"]),
                    "config_fees": money(item["config_fees"]),
                    "config_refunds": money(item["config_refunds"]),
                    "recognized_revenue": money(recognized),
                    "referral_rewards": money(item["referral_rewards"]),
                    "manual_adjustments": money(item["manual_adjustments"]),
                }
            )
        return result

    @staticmethod
    def _bucket(value: datetime, granularity: str, tz: ZoneInfo) -> date:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        local = value.astimezone(tz)
        if granularity == "month":
            return date(local.year, local.month, 1)
        if granularity == "week":
            return (local - timedelta(days=local.weekday())).date()
        return local.date()

    async def dashboard(
        self, *, period_from: datetime, period_to: datetime
    ) -> dict[str, Any]:
        overview = await self.overview(period_from=period_from, period_to=period_to)
        async with self._uow() as repos:
            session = repos["users"].session
            failed_operations = (
                await session.scalars(
                    select(VPNOperation)
                    .where(VPNOperation.status.in_(("failed", "pending", "running")))
                    .order_by(VPNOperation.updated_at.desc(), VPNOperation.id.desc())
                    .limit(10)
                )
            ).all()
            server_rows = (
                await session.execute(
                    select(
                        Server.id,
                        Server.name,
                        Server.location,
                        func.count(VPN_Config.id),
                        func.sum(
                            case((VPN_Config.actual_state == "active", 1), else_=0)
                        ),
                        func.sum(
                            case((VPN_Config.actual_state == "suspended", 1), else_=0)
                        ),
                    )
                    .outerjoin(VPN_Config, VPN_Config.server_id == Server.id)
                    .group_by(Server.id, Server.name, Server.location)
                    .order_by(Server.id)
                )
            ).all()
        overview["operations"] = {
            "attention": [
                {
                    "operation_id": row.operation_id,
                    "kind": row.kind,
                    "status": row.status,
                    "config_id": row.config_id,
                    "server_id": row.server_id,
                    "attempts": row.attempts,
                    "last_error": row.last_error,
                    "updated_at": utc_iso(row.updated_at),
                }
                for row in failed_operations
            ]
        }
        overview["servers"] = [
            {
                "id": row[0],
                "name": row[1],
                "location": row[2],
                "config_total": int(row[3] or 0),
                "config_active": int(row[4] or 0),
                "config_suspended": int(row[5] or 0),
            }
            for row in server_rows
        ]
        return overview
