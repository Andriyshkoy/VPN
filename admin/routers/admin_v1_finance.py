from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import aliased

from core.db.models import (
    BillingRun,
    LedgerEntry,
    ProviderPayment,
    ReferralReward,
    User,
)
from core.db.unit_of_work import uow
from core.services.admin_queries import money, numeric_search_predicates, utc_iso

from ..security import AdminPermission, AdminPrincipal, require_permission

router = APIRouter(prefix="/api/admin/v1/finance", tags=["admin-v1-finance"])

FinanceRead = Annotated[
    AdminPrincipal,
    Depends(require_permission(AdminPermission.FINANCE_READ)),
]

_SENSITIVE_DETAIL_PARTS = ("password", "secret", "token", "credential", "api_key")


def _safe_details(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            result[str(key)] = (
                "[redacted]"
                if any(part in normalized for part in _SENSITIVE_DETAIL_PARTS)
                else _safe_details(item)
            )
        return result
    if isinstance(value, list):
        return [_safe_details(item) for item in value]
    return value


def _user_search(q: str | None):
    normalized = (q or "").strip()
    if not normalized:
        return None
    escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    terms = [User.username.ilike(f"%{escaped}%", escape="\\")]
    terms.extend(
        numeric_search_predicates(
            normalized,
            integer_columns=(User.id,),
            bigint_columns=(User.tg_id,),
        )
    )
    return or_(*terms)


@router.get("/ledger")
async def list_ledger(
    _principal: FinanceRead,
    q: str | None = Query(default=None, max_length=128),
    user_id: int | None = Query(default=None, ge=1),
    direction: str | None = Query(default=None, pattern="^(credit|debit)$"),
    kind: str | None = Query(default=None, max_length=32),
    created_from: datetime | None = Query(default=None, alias="from"),
    created_to: datetime | None = Query(default=None, alias="to"),
    snapshot_id: int | None = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    conditions = []
    search = _user_search(q)
    if search is not None:
        conditions.append(search)
    if user_id is not None:
        conditions.append(LedgerEntry.user_id == user_id)
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

    async with uow() as repos:
        session = repos["users"].session
        if snapshot_id is None:
            snapshot_id = int(
                await session.scalar(select(func.max(LedgerEntry.id))) or 0
            )
        conditions.append(LedgerEntry.id <= snapshot_id)
        statement = select(LedgerEntry, User.username, User.tg_id).join(
            User, User.id == LedgerEntry.user_id
        )
        if conditions:
            statement = statement.where(*conditions)
        total = int(
            await session.scalar(
                select(func.count()).select_from(statement.order_by(None).subquery())
            )
            or 0
        )
        rows = (
            await session.execute(
                statement.order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc())
                .offset(offset)
                .limit(limit)
            )
        ).all()

    return {
        "items": [
            {
                "id": row.LedgerEntry.id,
                "user": {
                    "id": row.LedgerEntry.user_id,
                    "username": row.username,
                    "tg_id": row.tg_id,
                },
                "amount": money(row.LedgerEntry.amount),
                "balance_after": money(row.LedgerEntry.balance_after),
                "kind": row.LedgerEntry.kind,
                "reference_type": row.LedgerEntry.reference_type,
                "reference_id": row.LedgerEntry.reference_id,
                "details": _safe_details(dict(row.LedgerEntry.details or {})),
                "created_at": utc_iso(row.LedgerEntry.created_at),
            }
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
        "snapshot_id": snapshot_id,
    }


@router.get("/payments")
async def list_payments(
    _principal: FinanceRead,
    q: str | None = Query(default=None, max_length=128),
    user_id: int | None = Query(default=None, ge=1),
    payment_status: str | None = Query(default=None, alias="status", max_length=24),
    provider: str | None = Query(default=None, max_length=32),
    created_from: datetime | None = Query(default=None, alias="from"),
    created_to: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    conditions = []
    search = _user_search(q)
    if search is not None:
        conditions.append(search)
    if user_id is not None:
        conditions.append(ProviderPayment.user_id == user_id)
    if payment_status:
        conditions.append(ProviderPayment.status == payment_status)
    if provider:
        conditions.append(ProviderPayment.provider == provider)
    if created_from:
        conditions.append(ProviderPayment.created_at >= created_from)
    if created_to:
        conditions.append(ProviderPayment.created_at < created_to)
    statement = select(ProviderPayment, User.username, User.tg_id).join(
        User, User.id == ProviderPayment.user_id
    )
    if conditions:
        statement = statement.where(*conditions)
    async with uow() as repos:
        session = repos["users"].session
        total = int(
            await session.scalar(
                select(func.count()).select_from(statement.order_by(None).subquery())
            )
            or 0
        )
        rows = (
            await session.execute(
                statement.order_by(
                    ProviderPayment.created_at.desc(), ProviderPayment.id.desc()
                )
                .offset(offset)
                .limit(limit)
            )
        ).all()
    return {
        "items": [
            {
                "id": row.ProviderPayment.id,
                "intent_id": row.ProviderPayment.intent_id,
                "user": {
                    "id": row.ProviderPayment.user_id,
                    "username": row.username,
                    "tg_id": row.tg_id,
                },
                "provider": row.ProviderPayment.provider,
                "provider_payment_id": row.ProviderPayment.provider_payment_id,
                "amount": money(row.ProviderPayment.amount),
                "currency": row.ProviderPayment.currency,
                "status": row.ProviderPayment.status,
                "ledger_entry_id": row.ProviderPayment.ledger_entry_id,
                "created_at": utc_iso(row.ProviderPayment.created_at),
                "expires_at": utc_iso(row.ProviderPayment.expires_at),
                "credited_at": utc_iso(row.ProviderPayment.credited_at),
                "referral_settlement_status": (
                    row.ProviderPayment.referral_settlement_status
                ),
                "referral_settled_at": utc_iso(row.ProviderPayment.referral_settled_at),
            }
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/billing-runs")
async def list_billing_runs(
    _principal: FinanceRead,
    run_status: str | None = Query(default=None, alias="status", max_length=24),
    created_from: datetime | None = Query(default=None, alias="from"),
    created_to: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    conditions = []
    if run_status:
        conditions.append(BillingRun.status == run_status)
    if created_from:
        conditions.append(BillingRun.period_start >= created_from)
    if created_to:
        conditions.append(BillingRun.period_end < created_to)
    statement = select(BillingRun)
    if conditions:
        statement = statement.where(*conditions)
    async with uow() as repos:
        session = repos["users"].session
        total = int(
            await session.scalar(
                select(func.count()).select_from(statement.order_by(None).subquery())
            )
            or 0
        )
        rows = (
            await session.scalars(
                statement.order_by(BillingRun.period_end.desc(), BillingRun.id.desc())
                .offset(offset)
                .limit(limit)
            )
        ).all()
    return {
        "items": [
            {
                "id": row.id,
                "period_key": row.period_key,
                "period_start": utc_iso(row.period_start),
                "period_end": utc_iso(row.period_end),
                "cost_per_config": money(row.cost_per_config),
                "status": row.status,
                "charged_users": row.charged_users,
                "total_amount": money(row.total_amount),
                "created_at": utc_iso(row.created_at),
                "completed_at": utc_iso(row.completed_at),
            }
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/referral-rewards")
async def list_referral_rewards(
    _principal: FinanceRead,
    beneficiary_id: int | None = Query(default=None, ge=1),
    source_user_id: int | None = Query(default=None, ge=1),
    level: int | None = Query(default=None, ge=1, le=2),
    created_from: datetime | None = Query(default=None, alias="from"),
    created_to: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    source_user = aliased(User)
    beneficiary = aliased(User)
    conditions = []
    if beneficiary_id is not None:
        conditions.append(ReferralReward.beneficiary_user_id == beneficiary_id)
    if source_user_id is not None:
        conditions.append(ReferralReward.source_user_id == source_user_id)
    if level is not None:
        conditions.append(ReferralReward.level == level)
    if created_from:
        conditions.append(ReferralReward.created_at >= created_from)
    if created_to:
        conditions.append(ReferralReward.created_at < created_to)
    statement = (
        select(
            ReferralReward,
            source_user.username.label("source_username"),
            beneficiary.username.label("beneficiary_username"),
        )
        .join(source_user, source_user.id == ReferralReward.source_user_id)
        .join(beneficiary, beneficiary.id == ReferralReward.beneficiary_user_id)
    )
    if conditions:
        statement = statement.where(*conditions)
    async with uow() as repos:
        session = repos["users"].session
        total = int(
            await session.scalar(
                select(func.count()).select_from(statement.order_by(None).subquery())
            )
            or 0
        )
        rows = (
            await session.execute(
                statement.order_by(
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
                "beneficiary": {
                    "id": row.ReferralReward.beneficiary_user_id,
                    "username": row.beneficiary_username,
                },
                "level": row.ReferralReward.level,
                "rate_bps": row.ReferralReward.rate_bps,
                "source_amount": money(row.ReferralReward.source_amount),
                "reward_amount": money(row.ReferralReward.reward_amount),
                "currency": row.ReferralReward.currency,
                "ledger_entry_id": row.ReferralReward.ledger_entry_id,
                "program_version": row.ReferralReward.program_version,
                "created_at": utc_iso(row.ReferralReward.created_at),
            }
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
