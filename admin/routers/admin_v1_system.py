from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, literal, or_, select, union_all

from core.config import settings
from core.db.models import (
    AdminAction,
    AdminAuditEvent,
    AdminUser,
    BillingRun,
    NotificationOutbox,
    TelegramUpdateInbox,
    VPNOperation,
)
from core.db.unit_of_work import uow
from core.observability.manager_tls import inspect_manager_tls_material
from core.observability.snapshot import dependency_readiness
from core.services.admin_queries import money, numeric_search_predicates, utc_iso

from ..security import (
    AdminPermission,
    AdminPrincipal,
    require_any_permission,
    require_permission,
)

router = APIRouter(prefix="/api/admin/v1", tags=["admin-v1-system"])

AuditRead = Annotated[
    AdminPrincipal,
    Depends(require_permission(AdminPermission.AUDIT_READ)),
]
MetricsRead = Annotated[
    AdminPrincipal,
    Depends(require_permission(AdminPermission.METRICS_READ)),
]
OperationsRead = Annotated[
    AdminPrincipal,
    Depends(
        require_any_permission(
            AdminPermission.CONFIGS_READ,
            AdminPermission.SERVERS_READ,
        )
    ),
]

_SENSITIVE_KEYS = {
    "password",
    "password_hash",
    "token",
    "csrf_token",
    "api_key",
    "secret",
    "private_key",
    "raw_data",
    "authorization",
    "cookie",
    "set_cookie",
}


def _sensitive_key(value: object) -> bool:
    normalized = str(value).strip().lower().replace("-", "_")
    return (
        normalized in _SENSITIVE_KEYS
        or normalized.endswith(("_password", "_secret", "_token", "_api_key"))
        or "credential" in normalized
    )


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if _sensitive_key(key) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _age_seconds(value: datetime | None, now: datetime) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max(0, int((now - value.astimezone(timezone.utc)).total_seconds()))


async def _prometheus_alerts() -> tuple[list[dict[str, Any]], bool]:
    base_url = settings.prometheus_api_url.strip().rstrip("/")
    if not settings.observability_enabled or not base_url:
        return [], False
    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=min(settings.readiness_timeout_seconds, 5.0),
            trust_env=False,
        ) as client:
            response = await client.get("/api/v1/alerts")
            response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            return [], False
        raw_alerts = payload.get("data", {}).get("alerts", [])
        alerts = []
        for raw in raw_alerts[:100]:
            labels = raw.get("labels") if isinstance(raw, dict) else None
            annotations = raw.get("annotations") if isinstance(raw, dict) else None
            labels = labels if isinstance(labels, dict) else {}
            annotations = annotations if isinstance(annotations, dict) else {}
            name = str(labels.get("alertname") or "UnnamedAlert")[:160]
            instance = str(labels.get("instance") or "")[:160]
            alerts.append(
                {
                    "id": str(raw.get("fingerprint") or f"{name}:{instance}")[:256],
                    "name": name,
                    "severity": str(labels.get("severity") or "warning")[:32],
                    "state": str(raw.get("state") or "unknown")[:32],
                    "since": str(raw.get("activeAt") or "")[:64] or None,
                    "summary": str(
                        annotations.get("summary")
                        or annotations.get("description")
                        or ""
                    )[:1_000],
                    "labels": {
                        str(key)[:64]: str(value)[:160]
                        for key, value in labels.items()
                        if key in {"job", "instance", "service"}
                    },
                }
            )
        return alerts, True
    except (httpx.HTTPError, ValueError, TypeError):
        return [], False


@router.get("/audit-events")
async def list_audit_events(
    _principal: AuditRead,
    q: str | None = Query(default=None, max_length=128),
    actor_id: int | None = Query(default=None, ge=1),
    action: str | None = Query(default=None, max_length=96),
    target_type: str | None = Query(default=None, max_length=64),
    target_id: str | None = Query(default=None, max_length=160),
    result: str | None = Query(default=None, max_length=32),
    created_from: datetime | None = Query(default=None, alias="from"),
    created_to: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    statement = select(
        AdminAuditEvent, AdminUser.username.label("actor_username")
    ).outerjoin(AdminUser, AdminUser.id == AdminAuditEvent.actor_user_id)
    conditions = []
    normalized_q = (q or "").strip()
    if normalized_q:
        escaped = (
            normalized_q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        search_terms = [
            AdminAuditEvent.action.ilike(f"%{escaped}%", escape="\\"),
            AdminAuditEvent.target_type.ilike(f"%{escaped}%", escape="\\"),
            AdminAuditEvent.target_id.ilike(f"%{escaped}%", escape="\\"),
            AdminUser.username.ilike(f"%{escaped}%", escape="\\"),
        ]
        search_terms.extend(
            numeric_search_predicates(
                normalized_q,
                integer_columns=(AdminAuditEvent.actor_user_id,),
            )
        )
        conditions.append(or_(*search_terms))
    if actor_id is not None:
        conditions.append(AdminAuditEvent.actor_user_id == actor_id)
    if action:
        conditions.append(AdminAuditEvent.action == action)
    if target_type:
        conditions.append(AdminAuditEvent.target_type == target_type)
    if target_id:
        conditions.append(AdminAuditEvent.target_id == target_id)
    if result:
        conditions.append(
            or_(
                AdminAuditEvent.details["outcome"].as_string() == result,
                AdminAuditEvent.details["result"].as_string() == result,
            )
        )
    if created_from:
        conditions.append(AdminAuditEvent.created_at >= created_from)
    if created_to:
        conditions.append(AdminAuditEvent.created_at < created_to)
    if conditions:
        statement = statement.where(*conditions)
    count_stmt = select(func.count()).select_from(statement.order_by(None).subquery())
    page_stmt = (
        statement.order_by(AdminAuditEvent.created_at.desc(), AdminAuditEvent.id.desc())
        .offset(offset)
        .limit(limit)
    )
    async with uow() as repos:
        session = repos["users"].session
        total = int(await session.scalar(count_stmt) or 0)
        rows = (await session.execute(page_stmt)).all()
    return {
        "items": [_audit_payload(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _audit_payload(row) -> dict[str, Any]:
    event = row.AdminAuditEvent
    details = _redact(dict(event.details or {}))
    result = details.get("outcome") or details.get("result")
    if not result:
        result = "failed" if event.action.endswith(".failed") else "completed"
    actor = (
        {
            "id": event.actor_user_id,
            "username": row.actor_username,
        }
        if event.actor_user_id is not None
        else None
    )
    return {
        "id": event.id,
        "actor": actor,
        "actor_id": event.actor_user_id,
        "actor_username": row.actor_username,
        "action": event.action,
        "target_type": event.target_type,
        "target_id": event.target_id,
        "request_id": event.request_id,
        "correlation_id": event.correlation_id,
        "result": result,
        "reason": details.get("reason") or details.get("comment"),
        "details": details,
        "metadata": details,
        "created_at": utc_iso(event.created_at),
        "occurred_at": utc_iso(event.created_at),
    }


@router.get("/operations")
async def list_operations(
    principal: OperationsRead,
    operation_status: str | None = Query(default=None, alias="status", max_length=32),
    server_id: int | None = Query(default=None, ge=1),
    user_id: int | None = Query(default=None, ge=1),
    kind: str | None = Query(default=None, max_length=64),
    source: str | None = Query(default=None, pattern="^(vpn|server)$"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    can_read_vpn = AdminPermission.CONFIGS_READ in principal.permissions
    can_read_servers = AdminPermission.SERVERS_READ in principal.permissions
    if source == "vpn" and not can_read_vpn:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Config permission is required for VPN operations",
        )
    if source == "server" and not can_read_servers:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Server permission is required for server operations",
        )

    vpn_conditions = []
    server_conditions = []
    if operation_status:
        vpn_conditions.append(VPNOperation.status == operation_status)
        server_conditions.append(AdminAction.status == operation_status)
    if server_id is not None:
        vpn_conditions.append(VPNOperation.server_id == server_id)
        server_conditions.append(AdminAction.server_id == server_id)
    if user_id is not None:
        vpn_conditions.append(VPNOperation.owner_id == user_id)
        # Server actions have no VPN user target.
        server_conditions.append(literal(False))
    if kind:
        vpn_conditions.append(VPNOperation.kind == kind)
        server_conditions.append(AdminAction.kind == kind)

    vpn_statement = select(
        VPNOperation.id.label("database_id"),
        literal("vpn").label("source"),
        VPNOperation.operation_id.label("external_id"),
        VPNOperation.kind.label("kind"),
        VPNOperation.status.label("status"),
        VPNOperation.config_id.label("config_id"),
        VPNOperation.config_name.label("config_name"),
        VPNOperation.owner_id.label("user_id"),
        VPNOperation.server_id.label("server_id"),
        literal(None).label("actor_user_id"),
        VPNOperation.attempts.label("attempts"),
        VPNOperation.last_error.label("error"),
        VPNOperation.created_at.label("created_at"),
        VPNOperation.updated_at.label("updated_at"),
        VPNOperation.completed_at.label("completed_at"),
    )
    if vpn_conditions:
        vpn_statement = vpn_statement.where(*vpn_conditions)

    server_statement = select(
        AdminAction.id.label("database_id"),
        literal("server").label("source"),
        AdminAction.action_id.label("external_id"),
        AdminAction.kind.label("kind"),
        AdminAction.status.label("status"),
        literal(None).label("config_id"),
        literal(None).label("config_name"),
        literal(None).label("user_id"),
        AdminAction.server_id.label("server_id"),
        AdminAction.actor_user_id.label("actor_user_id"),
        literal(0).label("attempts"),
        AdminAction.error_detail.label("error"),
        AdminAction.created_at.label("created_at"),
        func.coalesce(
            AdminAction.completed_at,
            AdminAction.started_at,
            AdminAction.created_at,
        ).label("updated_at"),
        AdminAction.completed_at.label("completed_at"),
    )
    if server_conditions:
        server_statement = server_statement.where(*server_conditions)

    statements = []
    if can_read_vpn and source != "server":
        statements.append(vpn_statement)
    if can_read_servers and source != "vpn":
        statements.append(server_statement)
    timeline = (
        statements[0].subquery()
        if len(statements) == 1
        else union_all(*statements).subquery()
    )
    count_stmt = select(func.count()).select_from(timeline)
    page_stmt = (
        select(timeline)
        .order_by(
            timeline.c.updated_at.desc(),
            timeline.c.source,
            timeline.c.database_id.desc(),
        )
        .offset(offset)
        .limit(limit)
    )
    async with uow() as repos:
        session = repos["users"].session
        total = int(await session.scalar(count_stmt) or 0)
        rows = (await session.execute(page_stmt)).all()
    return {
        "items": [
            {
                "id": row.external_id,
                "database_id": row.database_id,
                "operation_id": row.external_id,
                "source": row.source,
                "kind": row.kind,
                "type": row.kind,
                "status": row.status,
                "config_id": row.config_id,
                "config_name": row.config_name,
                "owner_id": row.user_id,
                "user_id": row.user_id,
                "server_id": row.server_id,
                "actor_user_id": row.actor_user_id,
                "attempts": row.attempts,
                "last_error": row.error,
                "error": row.error,
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


@router.get("/observability/summary")
async def observability_summary(_principal: MetricsRead):
    dependencies = await dependency_readiness()
    alerts, prometheus_ready = await _prometheus_alerts()
    if settings.observability_enabled and settings.prometheus_api_url.strip():
        dependencies["prometheus"] = prometheus_ready
    tls = inspect_manager_tls_material()
    now = datetime.now(timezone.utc)
    async with uow() as repos:
        session = repos["users"].session
        operation_counts = dict(
            (
                await session.execute(
                    select(VPNOperation.status, func.count(VPNOperation.id)).group_by(
                        VPNOperation.status
                    )
                )
            ).all()
        )
        outbox_counts = dict(
            (
                await session.execute(
                    select(
                        NotificationOutbox.status,
                        func.count(NotificationOutbox.id),
                    ).group_by(NotificationOutbox.status)
                )
            ).all()
        )
        telegram_counts = dict(
            (
                await session.execute(
                    select(
                        TelegramUpdateInbox.status,
                        func.count(TelegramUpdateInbox.id),
                    ).group_by(TelegramUpdateInbox.status)
                )
            ).all()
        )
        last_billing = await session.scalar(
            select(BillingRun)
            .order_by(BillingRun.period_end.desc(), BillingRun.id.desc())
            .limit(1)
        )
        oldest_operation = await session.scalar(
            select(func.min(VPNOperation.created_at)).where(
                VPNOperation.status.in_(("pending", "running", "failed"))
            )
        )
        oldest_outbox = await session.scalar(
            select(func.min(NotificationOutbox.created_at)).where(
                NotificationOutbox.status.in_(("pending", "queued"))
            )
        )
        oldest_telegram = await session.scalar(
            select(func.min(TelegramUpdateInbox.received_at)).where(
                TelegramUpdateInbox.status.in_(("pending", "processing", "failed"))
            )
        )
    operation_counts = {str(key): int(value) for key, value in operation_counts.items()}
    outbox_counts = {str(key): int(value) for key, value in outbox_counts.items()}
    telegram_counts = {str(key): int(value) for key, value in telegram_counts.items()}
    queues = [
        {
            "name": "vpn_operations",
            "pending": sum(
                operation_counts.get(key, 0) for key in ("pending", "running", "failed")
            ),
            "failed": sum(
                operation_counts.get(key, 0) for key in ("failed", "exhausted")
            ),
            "oldest_age_seconds": _age_seconds(oldest_operation, now),
        },
        {
            "name": "notification_outbox",
            "pending": sum(outbox_counts.get(key, 0) for key in ("pending", "queued")),
            "failed": outbox_counts.get("failed", 0),
            "oldest_age_seconds": _age_seconds(oldest_outbox, now),
        },
        {
            "name": "telegram_inbox",
            "pending": sum(
                telegram_counts.get(key, 0)
                for key in ("pending", "processing", "failed")
            ),
            "failed": telegram_counts.get("dead", 0),
            "oldest_age_seconds": _age_seconds(oldest_telegram, now),
        },
    ]
    return {
        "generated_at": utc_iso(now),
        "status": "healthy" if all(dependencies.values()) else "degraded",
        "dependencies": dependencies,
        "features": {
            "maintenance": settings.maintenance_mode,
            "billing": settings.billing_enabled,
            "payments": settings.payments_enabled,
            "provisioning": settings.provisioning_enabled,
            "notifications": settings.notifications_enabled,
            "referral_rewards": settings.referral_rewards_enabled,
            "vpn_drift_repair": settings.vpn_drift_repair_enabled,
            "observability": settings.observability_enabled,
        },
        "manager_tls": {
            "enabled": tls.enabled,
            "ready": tls.ready,
            "certificate_expiry": {
                name: utc_iso(value) for name, value in tls.certificate_not_after
            },
        },
        "vpn_operations": operation_counts,
        "notification_outbox": outbox_counts,
        "telegram_inbox": telegram_counts,
        "queues": queues,
        "billing": (
            {
                "period_key": last_billing.period_key,
                "status": last_billing.status,
                "period_start": utc_iso(last_billing.period_start),
                "period_end": utc_iso(last_billing.period_end),
                "charged_users": last_billing.charged_users,
                "total_amount": money(last_billing.total_amount),
                "completed_at": utc_iso(last_billing.completed_at),
            }
            if last_billing is not None
            else None
        ),
        "alerts": alerts,
        "metrics": {
            "alerts_active": len(alerts),
            "vpn_operation_backlog": queues[0]["pending"],
            "notification_backlog": queues[1]["pending"],
            "telegram_update_backlog": queues[2]["pending"],
        },
        "links": [],
    }
