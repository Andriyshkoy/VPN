from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select

import core.db as db
from core.db.models.config import VPN_Config
from core.db.models.server import Server
from core.db.models.user import User
from core.db.models.vpn_operation import VPNOperation
from core.db.unit_of_work import uow
from core.exceptions import APIGatewayError, ConfigNotFoundError, InvalidOperationError
from core.services.admin_queries import numeric_search_predicates, utc_iso
from core.services.config import ConfigService

from ..schemas_v1 import (
    ConfigActionRequest,
    NodeConfigCreateRequest,
    NodeProfileResponse,
)
from ..security import (
    AdminPermission,
    AdminPrincipal,
    add_audit_event,
    require_permission,
)

router = APIRouter(prefix="/api/admin/v1/configs", tags=["admin-v1-configs"])
config_service = ConfigService(uow)

ConfigsRead = Annotated[
    AdminPrincipal,
    Depends(require_permission(AdminPermission.CONFIGS_READ)),
]
ConfigsWrite = Annotated[
    AdminPrincipal,
    Depends(require_permission(AdminPermission.CONFIGS_WRITE)),
]
ConfigsProvision = Annotated[
    AdminPrincipal,
    Depends(require_permission(AdminPermission.CONFIGS_PROVISION)),
]
ConfigsExport = Annotated[
    AdminPrincipal,
    Depends(require_permission(AdminPermission.CONFIGS_EXPORT)),
]
ProvisionKey = Annotated[UUID, Header(alias="Idempotency-Key")]


def _item(row) -> dict:
    cfg = row[0]
    return {
        "id": cfg.id,
        "name": cfg.name,
        "display_name": cfg.display_name,
        "owner": {
            "id": cfg.owner_id,
            "username": row.owner_username,
            "tg_id": row.owner_tg_id,
        },
        "server": {"id": cfg.server_id, "name": row.server_name},
        "created_at": utc_iso(cfg.created_at),
        "suspended": cfg.suspended,
        "suspended_at": utc_iso(cfg.suspended_at),
        "desired_state": cfg.desired_state,
        "actual_state": cfg.actual_state,
        "operation_id": cfg.operation_id,
        "operation_status": row.operation_status,
        "operation_attempts": int(row.operation_attempts or 0),
        "last_error": cfg.last_error,
        "service_id": cfg.service_id,
        "remote_access_id": (
            cfg.access_binding.remote_access_id if cfg.access_binding else None
        ),
        "updated_at": utc_iso(cfg.updated_at),
    }


@router.get("")
async def list_configs(
    _principal: ConfigsRead,
    q: str | None = Query(default=None, max_length=128),
    owner_id: int | None = Query(default=None, ge=1),
    server_id: int | None = Query(default=None, ge=1),
    state: str | None = Query(
        default=None,
        pattern="^(provisioning|active|suspended|revoked|failed|expired|limited)$",
    ),
    operation_status: str | None = Query(
        default=None,
        pattern="^(pending|running|succeeded|failed|rejected|superseded|exhausted)$",
    ),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    statement = (
        select(
            VPN_Config,
            User.username.label("owner_username"),
            User.tg_id.label("owner_tg_id"),
            Server.name.label("server_name"),
            VPNOperation.status.label("operation_status"),
            VPNOperation.attempts.label("operation_attempts"),
        )
        .join(User, User.id == VPN_Config.owner_id)
        .join(Server, Server.id == VPN_Config.server_id)
        .outerjoin(VPNOperation, VPNOperation.operation_id == VPN_Config.operation_id)
    )
    conditions = []
    normalized_q = (q or "").strip()
    if normalized_q:
        escaped = (
            normalized_q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        terms = [
            VPN_Config.name.ilike(f"%{escaped}%", escape="\\"),
            VPN_Config.display_name.ilike(f"%{escaped}%", escape="\\"),
            User.username.ilike(f"%{escaped}%", escape="\\"),
            Server.name.ilike(f"%{escaped}%", escape="\\"),
        ]
        terms.extend(
            numeric_search_predicates(
                normalized_q,
                integer_columns=(VPN_Config.id,),
            )
        )
        conditions.append(or_(*terms))
    if owner_id is not None:
        conditions.append(VPN_Config.owner_id == owner_id)
    if server_id is not None:
        conditions.append(VPN_Config.server_id == server_id)
    if state:
        conditions.append(VPN_Config.actual_state == state)
    if operation_status:
        conditions.append(VPNOperation.status == operation_status)
    if conditions:
        statement = statement.where(*conditions)

    count_stmt = select(func.count()).select_from(statement.order_by(None).subquery())
    page_stmt = (
        statement.order_by(VPN_Config.updated_at.desc(), VPN_Config.id.desc())
        .offset(offset)
        .limit(limit)
    )
    async with uow() as repos:
        session = repos["users"].session
        total = int(await session.scalar(count_stmt) or 0)
        rows = (await session.execute(page_stmt)).all()
    return {
        "items": [_item(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{config_id}")
async def get_config(config_id: int, _principal: ConfigsRead):
    statement = (
        select(
            VPN_Config,
            User.username.label("owner_username"),
            User.tg_id.label("owner_tg_id"),
            Server.name.label("server_name"),
            VPNOperation.status.label("operation_status"),
            VPNOperation.attempts.label("operation_attempts"),
        )
        .join(User, User.id == VPN_Config.owner_id)
        .join(Server, Server.id == VPN_Config.server_id)
        .outerjoin(VPNOperation, VPNOperation.operation_id == VPN_Config.operation_id)
        .where(VPN_Config.id == config_id)
    )
    async with uow() as repos:
        row = (await repos["users"].session.execute(statement)).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Config not found"
        )
    return _item(row)


async def _record_action(
    request: Request,
    principal: AdminPrincipal,
    *,
    config_id: int,
    action: str,
    reason: str,
    outcome: str,
    error: str | None = None,
) -> None:
    async with db.async_session() as session, session.begin():
        add_audit_event(
            session,
            request,
            action=f"config.{action}",
            actor_user_id=principal.user_id,
            target_type="vpn_config",
            target_id=config_id,
            details={
                "reason": reason,
                "outcome": outcome,
                "error": error,
            },
        )


async def _record_provision(
    request: Request,
    principal: AdminPrincipal,
    data: NodeConfigCreateRequest,
    *,
    operation_id: str,
    config_id: int | None,
    outcome: str,
    error: str | None = None,
) -> None:
    async with db.async_session() as session, session.begin():
        add_audit_event(
            session,
            request,
            action="config.provision",
            actor_user_id=principal.user_id,
            target_type="vpn_config",
            target_id=config_id,
            details={
                "operation_id": operation_id,
                "server_id": data.server_id,
                "owner_id": data.owner_id,
                "service_id": data.service_id,
                "reason": data.reason,
                "outcome": outcome,
                "error": error,
            },
        )


@router.post("", status_code=status.HTTP_201_CREATED)
async def provision_node_config(
    data: NodeConfigCreateRequest,
    request: Request,
    principal: ConfigsProvision,
    idempotency_key: ProvisionKey,
):
    """Create an operator-managed technical access without a customer charge."""

    operation_id = str(idempotency_key)
    try:
        result = await config_service.create_config(
            server_id=data.server_id,
            owner_id=data.owner_id,
            name=data.name,
            display_name=data.display_name,
            service_id=data.service_id,
            access_policy=data.access_policy.model_dump(mode="json"),
            operation_id=operation_id,
        )
    except InvalidOperationError as exc:
        await _record_provision(
            request,
            principal,
            data,
            operation_id=operation_id,
            config_id=None,
            outcome="rejected",
            error=type(exc).__name__,
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except APIGatewayError as exc:
        async with uow() as repos:
            operation = await repos["vpn_operations"].get(operation_id=operation_id)
            config_id = operation.config_id if operation else None
            retryable = bool(
                operation and operation.status in {"pending", "running", "failed"}
            )
        outcome = "queued_for_reconciliation" if retryable else "failed"
        await _record_provision(
            request,
            principal,
            data,
            operation_id=operation_id,
            config_id=config_id,
            outcome=outcome,
            error=type(exc).__name__,
        )
        return JSONResponse(
            {"config_id": config_id, "operation_id": operation_id, "state": outcome},
            status_code=202 if retryable else 502,
            headers={"Cache-Control": "no-store"},
        )
    operation_status = result.operation_status
    outcome = (
        "completed"
        if operation_status == "succeeded"
        else (
            "failed"
            if operation_status in {"rejected", "exhausted", "superseded"}
            else "queued_for_reconciliation"
        )
    )
    await _record_provision(
        request,
        principal,
        data,
        operation_id=operation_id,
        config_id=result.id,
        outcome=outcome,
    )
    payload = {
        "config_id": result.id,
        "operation_id": operation_id,
        "state": outcome,
        "service_id": result.service_id,
        "remote_access_id": result.remote_access_id,
        "desired_state": result.desired_state,
        "actual_state": result.actual_state,
    }
    if outcome != "completed":
        return JSONResponse(
            payload,
            status_code=202 if outcome == "queued_for_reconciliation" else 502,
            headers={"Cache-Control": "no-store"},
        )
    return payload


@router.get("/{config_id}/profile", response_model=NodeProfileResponse)
async def export_node_profile(
    config_id: int,
    request: Request,
    response: Response,
    principal: ConfigsExport,
):
    """Explicit secret export; ordinary config reads never include the URI."""

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    try:
        profile = await config_service.export_profile(config_id)
    except ConfigNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail="Config not found",
            headers={"Cache-Control": "no-store"},
        ) from exc
    except InvalidOperationError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
            headers={"Cache-Control": "no-store"},
        ) from exc
    except APIGatewayError as exc:
        raise HTTPException(
            status_code=503,
            detail="Profile export is unavailable",
            headers={"Cache-Control": "no-store"},
        ) from exc
    await _record_action(
        request,
        principal,
        config_id=config_id,
        action="export_profile",
        reason="Explicit operator secret export",
        outcome="completed",
    )
    return NodeProfileResponse(
        config_id=config_id,
        access_id=profile.access_id,
        service_id=profile.service_id,
        format=profile.format,
        uri=profile.uri,
        content=profile.content,
    )


@router.post("/{config_id}/actions")
async def config_action(
    config_id: int,
    data: ConfigActionRequest,
    request: Request,
    principal: ConfigsWrite,
):
    try:
        if data.action == "suspend":
            result = await config_service.suspend_config(config_id)
        elif data.action == "unsuspend":
            result = await config_service.unsuspend_config(config_id)
        else:
            await config_service.revoke_config(config_id)
            result = await config_service.get(config_id)
    except ConfigNotFoundError as exc:
        await _record_action(
            request,
            principal,
            config_id=config_id,
            action=data.action,
            reason=data.reason,
            outcome="not_found",
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Config not found",
        ) from exc
    except InvalidOperationError as exc:
        await _record_action(
            request,
            principal,
            config_id=config_id,
            action=data.action,
            reason=data.reason,
            outcome="rejected",
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except APIGatewayError as exc:
        await _record_action(
            request,
            principal,
            config_id=config_id,
            action=data.action,
            reason=data.reason,
            outcome="queued_for_reconciliation",
            error=type(exc).__name__,
        )
        return {
            "config_id": config_id,
            "action": data.action,
            "state": "queued_for_reconciliation",
        }

    await _record_action(
        request,
        principal,
        config_id=config_id,
        action=data.action,
        reason=data.reason,
        outcome="completed",
    )
    return {
        "config_id": config_id,
        "action": data.action,
        "state": "completed",
        "config": (
            {
                "desired_state": result.desired_state,
                "actual_state": result.actual_state,
                "operation_id": result.operation_id,
                "last_error": result.last_error,
            }
            if result is not None
            else None
        ),
    }
