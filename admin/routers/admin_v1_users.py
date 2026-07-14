from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

import core.db as db
from core.db.unit_of_work import uow
from core.exceptions import (
    InsufficientBalanceError,
    InvalidOperationError,
    UserNotFoundError,
)
from core.services.admin_queries import (
    AdminReferralQueryService,
    AdminUserQueryService,
    money,
)
from core.services.user_timeline import AdminUserTimelineService

from ..schemas_v1 import BalanceAdjustmentRequest
from ..security import (
    AdminPermission,
    AdminPrincipal,
    add_audit_event,
    require_permission,
)
from ..services_v1 import (
    AdminBalanceService,
    AdminIdempotencyConflict,
    AdminOptimisticConflict,
    BalanceAdjustmentCommand,
)

router = APIRouter(prefix="/api/admin/v1/users", tags=["admin-v1-users"])
users = AdminUserQueryService(uow)
referrals = AdminReferralQueryService(uow)
balances = AdminBalanceService()
timeline = AdminUserTimelineService(uow)

UsersRead = Annotated[
    AdminPrincipal,
    Depends(require_permission(AdminPermission.USERS_READ)),
]
TimelineRead = Annotated[
    AdminPrincipal,
    Depends(
        require_permission(
            AdminPermission.USERS_READ,
            AdminPermission.AUDIT_READ,
        )
    ),
]
BalanceRead = Annotated[
    AdminPrincipal,
    Depends(
        require_permission(
            AdminPermission.USERS_READ,
            AdminPermission.BALANCE_READ,
        )
    ),
]
BalanceWrite = Annotated[
    AdminPrincipal,
    Depends(require_permission(AdminPermission.BALANCE_WRITE)),
]
ReferralsRead = Annotated[
    AdminPrincipal,
    Depends(require_permission(AdminPermission.REFERRALS_READ)),
]
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=160),
]


async def _audit_balance_rejection(
    *,
    request: Request,
    principal: AdminPrincipal,
    user_id: int,
    data: BalanceAdjustmentRequest,
    idempotency_key: str,
    error: Exception,
) -> None:
    async with db.async_session() as session, session.begin():
        add_audit_event(
            session,
            request,
            action="balance.adjustment_rejected",
            actor_user_id=principal.user_id,
            target_type="user",
            target_id=user_id,
            details={
                "outcome": "rejected",
                "error_code": type(error).__name__,
                "direction": data.direction,
                "amount": money(data.amount),
                "reason_code": data.reason_code,
                "comment": data.comment,
                "idempotency_key_hash": hashlib.sha256(
                    idempotency_key.encode("utf-8")
                ).hexdigest(),
            },
        )


@router.get("")
async def list_users(
    principal: UsersRead,
    q: str | None = Query(default=None, max_length=128),
    delivery_status: str | None = Query(
        default=None,
        pattern="^(active|blocked|deactivated|permanent_failure)$",
    ),
    has_configs: bool | None = None,
    config_state: str | None = Query(
        default=None, pattern="^(active|suspended|pending|failed)$"
    ),
    has_payments: bool | None = None,
    referrer_id: int | None = Query(default=None, ge=1),
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    sort: str = Query(
        default="created_at", pattern="^(created_at|balance|last_payment_at|username)$"
    ),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    if AdminPermission.BALANCE_READ not in principal.permissions and (
        has_payments is not None or sort in {"balance", "last_payment_at"}
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Balance permission is required for this filter or sort",
        )
    if (
        AdminPermission.REFERRALS_READ not in principal.permissions
        and referrer_id is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Referral permission is required for this filter",
        )
    payload = await users.list_users(
        q=q,
        delivery_status=delivery_status,
        has_configs=has_configs,
        config_state=config_state,
        has_payments=has_payments,
        referrer_id=referrer_id,
        created_from=created_from,
        created_to=created_to,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
    )
    for item in payload["items"]:
        if AdminPermission.BALANCE_READ not in principal.permissions:
            item.pop("balance", None)
            item.pop("credited_total", None)
            item.pop("last_payment_at", None)
        if AdminPermission.REFERRALS_READ not in principal.permissions:
            item.pop("referrer", None)
            item.pop("direct_referrals", None)
    return payload


@router.get("/{user_id}")
async def get_user(user_id: int, principal: UsersRead):
    payload = await users.get_user(user_id)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    if AdminPermission.BALANCE_READ not in principal.permissions:
        payload.pop("finance", None)
    if AdminPermission.REFERRALS_READ not in principal.permissions:
        payload.pop("referral", None)
        payload.get("identity", {}).pop("referral_code", None)
    return payload


@router.get("/{user_id}/timeline")
async def get_user_timeline(
    user_id: int,
    request: Request,
    principal: TimelineRead,
    category: str | None = Query(
        default=None,
        pattern="^(bot|finance|referral|vpn|admin|account)$",
    ),
    action: str | None = Query(default=None, min_length=1, max_length=96),
    result: str | None = Query(default=None, min_length=1, max_length=32),
    occurred_from: datetime | None = Query(default=None, alias="from"),
    occurred_to: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
):
    raw_to = request.query_params.get("to")
    if (
        occurred_to is not None
        and raw_to
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_to)
    ):
        # A date selected in the UI means the entire calendar day; explicit
        # timestamps retain the documented exclusive upper-bound semantics.
        try:
            occurred_to += timedelta(days=1)
        except OverflowError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="timeline 'to' date is out of range",
            ) from exc
    try:
        payload = await timeline.list_timeline(
            user_id,
            category=category,
            action=action,
            result=result,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            limit=limit,
            offset=offset,
            include_finance=bool(
                principal.permissions
                & {AdminPermission.BALANCE_READ, AdminPermission.FINANCE_READ}
            ),
            include_referral=(AdminPermission.REFERRALS_READ in principal.permissions),
            include_vpn=(AdminPermission.CONFIGS_READ in principal.permissions),
            include_admin=(AdminPermission.AUDIT_READ in principal.permissions),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return payload


@router.get("/{user_id}/ledger")
async def get_user_ledger(
    user_id: int,
    _principal: BalanceRead,
    direction: str | None = Query(default=None, pattern="^(credit|debit)$"),
    kind: str | None = Query(default=None, max_length=32),
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    snapshot_id: int | None = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    payload = await users.list_ledger(
        user_id,
        direction=direction,
        kind=kind,
        created_from=created_from,
        created_to=created_to,
        snapshot_id=snapshot_id,
        limit=limit,
        offset=offset,
    )
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    return payload


@router.get("/{user_id}/payments")
async def get_user_payments(
    user_id: int,
    _principal: BalanceRead,
    payment_status: str | None = Query(default=None, alias="status", max_length=24),
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    payload = await users.list_payments(
        user_id,
        status=payment_status,
        created_from=created_from,
        created_to=created_to,
        limit=limit,
        offset=offset,
    )
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    return payload


@router.get("/{user_id}/configs")
async def get_user_configs(
    user_id: int,
    _principal: UsersRead,
    state: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    payload = await users.list_configs(
        user_id,
        state=state,
        limit=limit,
        offset=offset,
    )
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    return payload


@router.get("/{user_id}/vpn-operations")
async def get_user_vpn_operations(
    user_id: int,
    _principal: UsersRead,
    operation_status: str | None = Query(default=None, alias="status", max_length=32),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    payload = await users.list_vpn_operations(
        user_id,
        status=operation_status,
        limit=limit,
        offset=offset,
    )
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    return payload


@router.get("/{user_id}/referrals/ancestry")
async def get_referral_ancestry(
    user_id: int,
    _principal: ReferralsRead,
    max_depth: int = Query(default=50, ge=1, le=50),
):
    payload = await referrals.ancestry(user_id, max_depth=max_depth)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    return {"items": payload}


@router.get("/{user_id}/referrals/children")
async def get_referral_children(
    user_id: int,
    _principal: ReferralsRead,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    payload = await referrals.children(user_id, limit=limit, offset=offset)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    return payload


@router.get("/{user_id}/referral-rewards")
async def get_referral_rewards(
    user_id: int,
    _principal: ReferralsRead,
    level: int | None = Query(default=None, ge=1, le=2),
    source_user_id: int | None = Query(default=None, ge=1),
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    payload = await referrals.rewards(
        user_id,
        level=level,
        source_user_id=source_user_id,
        created_from=created_from,
        created_to=created_to,
        limit=limit,
        offset=offset,
    )
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    return payload


@router.post("/{user_id}/balance-adjustments")
async def adjust_user_balance(
    user_id: int,
    data: BalanceAdjustmentRequest,
    request: Request,
    principal: BalanceWrite,
    idempotency_key: IdempotencyKey,
):
    try:
        return await balances.adjust(
            request=request,
            principal=principal,
            user_id=user_id,
            client_key=idempotency_key,
            command=BalanceAdjustmentCommand(
                direction=data.direction,
                amount=data.amount,
                reason_code=data.reason_code,
                comment=data.comment,
                expected_balance=data.expected_balance,
                expected_ledger_entry_id=data.expected_ledger_entry_id,
            ),
        )
    except UserNotFoundError as exc:
        await _audit_balance_rejection(
            request=request,
            principal=principal,
            user_id=user_id,
            data=data,
            idempotency_key=idempotency_key,
            error=exc,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        ) from exc
    except (
        AdminIdempotencyConflict,
        AdminOptimisticConflict,
        InsufficientBalanceError,
    ) as exc:
        await _audit_balance_rejection(
            request=request,
            principal=principal,
            user_id=user_id,
            data=data,
            idempotency_key=idempotency_key,
            error=exc,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except InvalidOperationError as exc:
        await _audit_balance_rejection(
            request=request,
            principal=principal,
            user_id=user_id,
            data=data,
            idempotency_key=idempotency_key,
            error=exc,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
