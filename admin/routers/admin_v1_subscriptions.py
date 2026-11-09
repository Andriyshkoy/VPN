"""Subscription controls and explicit, audited account billing exceptions."""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select

from core.config import settings
from core.db.models import AdminAuditEvent, User
from core.db.models.subscription import Subscription
from core.db.unit_of_work import uow
from core.exceptions import InvalidOperationError, UserNotFoundError
from core.services.admin_queries import money

from ..schemas_product import SubscriptionPlanRequest, UserFlagRequest
from ..security import (
    AdminPermission,
    AdminPrincipal,
    add_audit_event,
    require_permission,
)
from ..services_product import AdminUserFlagService

router = APIRouter(
    prefix="/api/admin/v1/subscriptions", tags=["admin-v1-subscriptions"]
)
users_router = APIRouter(prefix="/api/admin/v1/users", tags=["admin-v1-subscriptions"])
flags = AdminUserFlagService()
SubscriptionRead = Annotated[
    AdminPrincipal, Depends(require_permission(AdminPermission.SUBSCRIPTIONS_READ))
]
SubscriptionWrite = Annotated[
    AdminPrincipal, Depends(require_permission(AdminPermission.SUBSCRIPTIONS_WRITE))
]
BalanceWrite = Annotated[
    AdminPrincipal, Depends(require_permission(AdminPermission.BALANCE_WRITE))
]
OwnerWrite = Annotated[
    AdminPrincipal, Depends(require_permission(AdminPermission.ADMINS_MANAGE))
]
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=8, max_length=160)
]


def _service(factory=uow):
    from core.services.subscriptions import SubscriptionService

    return SubscriptionService(factory)


def _payload(view):
    payload = jsonable_encoder(view, custom_encoder={Decimal: money})
    if isinstance(payload, dict) and "max_sessions" in payload:
        payload["max_hysteria_sessions"] = payload["max_sessions"]
    return payload


def _visible_subscription(view, principal):
    payload = _payload(view)
    if (
        isinstance(payload, dict)
        and AdminPermission.BALANCE_READ not in principal.permissions
    ):
        payload.pop("unlimited_balance", None)
        payload.pop("next_charge_at", None)
    return payload


def _domain_error(exc):
    if isinstance(exc, UserNotFoundError):
        raise HTTPException(404, detail="User not found") from exc
    raise HTTPException(409, detail=str(exc)) from exc


@router.get("/plans")
async def get_plans(_principal: SubscriptionRead):
    return {"items": [_payload(plan) for plan in _service().catalog()]}


@router.get("/migration/preview")
async def migration_preview(_principal: SubscriptionRead):
    from core.services.subscription_migration import SubscriptionMigrationService

    candidates = await SubscriptionMigrationService(uow).preview()
    items = []
    counts = {"basic": 0, "plus": 0, "suspended": 0, "unchanged": 0, "total": 0}
    for row in candidates:
        status = row.status if row.status in {"active", "suspended"} else "unchanged"
        if row.existing_subscription_id:
            counts["unchanged"] += 1
        elif status == "suspended":
            counts["suspended"] += 1
        elif row.plan_code in {"basic", "plus"}:
            counts[row.plan_code] += 1
        else:
            counts["unchanged"] += 1
        counts["total"] += 1
        items.append(
            {
                "user_id": row.user_id,
                "active_configs": row.active_config_count,
                "plan_code": row.plan_code,
                "status": status,
                "reason": row.reason,
                "eligible": row.eligible,
                "existing_subscription_id": row.existing_subscription_id,
            }
        )
    return {
        "billing_enabled": bool(settings.billing_enabled),
        "read_only": True,
        "counts": counts,
        "items": items,
        "balance_changes": 0,
        "ledger_changes": 0,
        "unlimited_balance_changes": 0,
    }


@router.get("")
async def list_subscriptions(
    principal: SubscriptionRead,
    plan_code: Literal["basic", "plus"] | None = None,
    status: (
        Literal["pending", "active", "suspended", "expired", "cancelled"] | None
    ) = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    async with uow() as repos:
        session = repos["users"].session
        filters = []
        if plan_code is not None:
            filters.append(Subscription.plan_code == plan_code)
        if status is not None:
            filters.append(Subscription.status == status)
        total = int(
            await session.scalar(
                select(func.count()).select_from(Subscription).where(*filters)
            )
            or 0
        )
        user_ids = list(
            await session.scalars(
                select(Subscription.user_id)
                .where(*filters)
                .order_by(Subscription.user_id)
                .limit(limit)
                .offset(offset)
            )
        )

        @asynccontextmanager
        async def bound_uow():
            yield repos

        service = _service(bound_uow)
        items = [
            _visible_subscription(await service.get_current(user_id), principal)
            for user_id in user_ids
        ]
        return {"items": items, "total": total, "limit": limit, "offset": offset}


@users_router.get("/{user_id}/subscription")
async def get_subscription(user_id: int, principal: SubscriptionRead):
    try:
        async with uow() as repos:
            if await repos["users"].session.get(User, user_id) is None:
                raise UserNotFoundError(user_id)

            @asynccontextmanager
            async def bound_uow():
                yield repos

            return {
                "subscription": _visible_subscription(
                    await _service(bound_uow).get_current(user_id), principal
                )
            }
    except (UserNotFoundError, InvalidOperationError) as exc:
        _domain_error(exc)


@users_router.patch("/{user_id}/subscription")
async def change_subscription(
    user_id: int,
    data: SubscriptionPlanRequest,
    request: Request,
    principal: SubscriptionWrite,
    idempotency_key: IdempotencyKey,
):
    command = {"user_id": user_id, **data.model_dump()}
    key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
    request_hash = hashlib.sha256(
        json.dumps(command, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    try:
        async with uow() as repos:
            session = repos["users"].session
            user = await session.scalar(
                select(User).where(User.id == user_id).with_for_update()
            )
            if user is None:
                raise UserNotFoundError(user_id)
            previous = await session.scalar(
                select(AdminAuditEvent).where(
                    AdminAuditEvent.action == "subscription.plan_changed",
                    AdminAuditEvent.target_type == "user",
                    AdminAuditEvent.target_id == str(user_id),
                    AdminAuditEvent.details["idempotency_key_hash"].as_string()
                    == key_hash,
                )
            )
            if previous is not None:
                if previous.details.get("request_hash") != request_hash:
                    raise InvalidOperationError(
                        "Idempotency-Key was used for a different plan change"
                    )
                return {**previous.details["result"], "replayed": True}

            @asynccontextmanager
            async def bound_uow():
                yield repos

            before = await _service(bound_uow).get_current(user_id)
            view = await _service(bound_uow).select_plan(
                user_id,
                data.plan_code,
                request_key=f"admin:{user_id}:{key_hash}",
                expected_version=data.expected_version,
            )
            result = _payload(view)
            add_audit_event(
                session,
                request,
                action="subscription.plan_changed",
                actor_user_id=principal.user_id,
                target_type="user",
                target_id=user_id,
                details={
                    "before": _payload(before),
                    "result": result,
                    "reason": data.reason,
                    "idempotency_key_hash": key_hash,
                    "request_hash": request_hash,
                    "wallet_changed": False,
                    "ledger_changed": False,
                },
            )
            return result
    except (UserNotFoundError, InvalidOperationError) as exc:
        _domain_error(exc)


@users_router.patch("/{user_id}/unlimited-balance")
async def set_unlimited_balance(
    user_id: int,
    data: UserFlagRequest,
    request: Request,
    principal: BalanceWrite,
    idempotency_key: IdempotencyKey,
):
    try:
        return await flags.set_flag(
            request=request,
            principal=principal,
            user_id=user_id,
            client_key=idempotency_key,
            flag="unlimited_balance",
            **data.model_dump(),
        )
    except (UserNotFoundError, InvalidOperationError) as exc:
        _domain_error(exc)


@users_router.patch("/{user_id}/business-metrics-exclusion")
async def set_metrics_exclusion(
    user_id: int,
    data: UserFlagRequest,
    request: Request,
    principal: OwnerWrite,
    idempotency_key: IdempotencyKey,
):
    try:
        return await flags.set_flag(
            request=request,
            principal=principal,
            user_id=user_id,
            client_key=idempotency_key,
            flag="exclude_from_business_metrics",
            **data.model_dump(),
        )
    except (UserNotFoundError, InvalidOperationError) as exc:
        _domain_error(exc)
