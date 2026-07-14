from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

from ..fleet_schemas import (
    AdminServerActionRequest,
    AdminServerCreate,
    AdminServerUpdate,
)
from ..fleet_service import AdminFleetRemoteError, AdminFleetService
from ..security import AdminPermission, AdminPrincipal, require_permission

router = APIRouter(prefix="/api/admin/v1/servers", tags=["admin-v1-servers"])
operations_router = APIRouter(
    prefix="/api/admin/v1/server-actions", tags=["admin-v1-server-actions"]
)
fleet = AdminFleetService()

ServersRead = Annotated[
    AdminPrincipal,
    Depends(require_permission(AdminPermission.SERVERS_READ)),
]
ServersWrite = Annotated[
    AdminPrincipal,
    Depends(require_permission(AdminPermission.SERVERS_WRITE)),
]
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=160),
]


def _project_server_finance(payload: dict, principal: AdminPrincipal) -> dict:
    if not {
        AdminPermission.FINANCE_READ,
        AdminPermission.SERVERS_WRITE,
    }.intersection(principal.permissions):
        payload.pop("monthly_cost", None)
    return payload


@router.get("")
async def list_servers(
    principal: ServersRead,
    q: str | None = Query(default=None, max_length=128),
    lifecycle_state: str | None = Query(
        default=None, pattern="^(active|draining|disabled|retired)$"
    ),
    server_status: str | None = Query(
        default=None,
        alias="status",
        pattern=(
            "^(active|draining|disabled|retired|healthy|unhealthy|unreachable|"
            "unknown|instance_mismatch|stale)$"
        ),
    ),
    location: str | None = Query(default=None, max_length=128),
    provider: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    selected_lifecycle = lifecycle_state
    health_state = None
    if server_status in {"active", "draining", "disabled", "retired"}:
        selected_lifecycle = server_status
    elif server_status:
        health_state = server_status
    payload = await fleet.list_servers(
        q=q,
        lifecycle_state=selected_lifecycle,
        health_state=health_state,
        location=location,
        provider=provider,
        limit=limit,
        offset=offset,
    )
    for item in payload["items"]:
        _project_server_finance(item, principal)
    return payload


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_server(
    data: AdminServerCreate,
    request: Request,
    principal: ServersWrite,
):
    return await fleet.create_server(request=request, principal=principal, data=data)


@router.get("/{server_id}")
async def get_server(server_id: int, principal: ServersRead):
    payload = await fleet.get_server(server_id)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Server not found"
        )
    return _project_server_finance(payload, principal)


@router.patch("/{server_id}")
async def update_server(
    server_id: int,
    data: AdminServerUpdate,
    request: Request,
    principal: ServersWrite,
):
    return await fleet.update_server(
        server_id, request=request, principal=principal, data=data
    )


@router.get("/{server_id}/status")
async def get_server_status(server_id: int, _principal: ServersRead):
    payload = await fleet.get_latest_status(server_id)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Server not found"
        )
    return payload


@router.get("/{server_id}/status/history")
async def get_server_status_history(
    server_id: int,
    _principal: ServersRead,
    kind: str | None = Query(default=None, pattern="^(status|inventory)$"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    payload = await fleet.status_history(
        server_id, kind=kind, limit=limit, offset=offset
    )
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Server not found"
        )
    return payload


@router.post("/{server_id}/actions")
async def execute_server_action(
    server_id: int,
    data: AdminServerActionRequest,
    request: Request,
    principal: ServersWrite,
    idempotency_key: IdempotencyKey,
):
    try:
        return await fleet.execute_action(
            server_id,
            request=request,
            principal=principal,
            client_key=idempotency_key,
            command=data,
        )
    except AdminFleetRemoteError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="VPN Manager request failed; the action is recorded for audit",
        ) from exc


@operations_router.get("")
async def list_operations(
    _principal: ServersRead,
    server_id: int | None = Query(default=None, ge=1),
    operation_status: str | None = Query(default=None, alias="status", max_length=16),
    kind: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    return await fleet.list_actions(
        server_id=server_id,
        status=operation_status,
        kind=kind,
        limit=limit,
        offset=offset,
    )


@operations_router.get("/{action_id}")
async def get_operation(action_id: str, _principal: ServersRead):
    payload = await fleet.get_action(action_id)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Operation not found"
        )
    return payload
