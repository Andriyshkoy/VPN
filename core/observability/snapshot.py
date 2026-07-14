from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import case, func, select, text

import core.db as db
from core.config import settings
from core.db.models.billing_run import BillingRun
from core.db.models.config import VPN_Config
from core.db.models.notification_outbox import NotificationOutbox
from core.db.models.server import Server, VPNServerStatus
from core.db.models.telegram_update import TelegramUpdateInbox
from core.db.models.vpn_operation import VPNOperation
from core.db.schema import EXPECTED_ALEMBIC_REVISION
from core.domain import (
    ServerLifecycleState,
    TelegramUpdateStatus,
    VPNOperationStatus,
    VPNState,
)

from .manager_tls import (
    ManagerTLSStatus,
    inspect_manager_tls_material,
    manager_tls_ready,
)

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
_BILLING_STATUSES = ("running", "completed")
_OUTBOX_STATUSES = ("pending", "queued", "delivered", "failed")
_OUTBOX_BACKLOG_STATUSES = ("pending", "queued")
_VPN_BACKLOG_STATUSES = (
    VPNOperationStatus.PENDING.value,
    VPNOperationStatus.RUNNING.value,
    VPNOperationStatus.FAILED.value,
)
_TELEGRAM_BACKLOG_STATUSES = (
    TelegramUpdateStatus.PENDING.value,
    TelegramUpdateStatus.PROCESSING.value,
    TelegramUpdateStatus.FAILED.value,
)
_PROCESS_START_TIMESTAMP_SECONDS = time.time()
_FLEET_LIFECYCLE_STATES = ("active", "draining", "disabled", "retired")
_FLEET_HEALTH_STATES = (
    "healthy",
    "unhealthy",
    "unreachable",
    "stale",
    "unknown",
    "instance_mismatch",
    "disabled",
    "retired",
)


async def database_ready() -> bool:
    try:
        async with asyncio.timeout(settings.readiness_timeout_seconds):
            async with db.async_session() as session:
                await session.execute(text("SELECT 1"))
                revisions = (
                    await session.scalars(
                        text("SELECT version_num FROM alembic_version")
                    )
                ).all()
        return set(revisions) == {EXPECTED_ALEMBIC_REVISION}
    except Exception:
        return False


async def redis_ready() -> bool:
    client = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=settings.readiness_timeout_seconds,
        socket_timeout=settings.readiness_timeout_seconds,
    )
    try:
        async with asyncio.timeout(settings.readiness_timeout_seconds):
            return bool(await client.ping())
    except Exception:
        return False
    finally:
        await client.aclose()


async def dependency_readiness() -> dict[str, bool]:
    database, redis, manager_tls = await asyncio.gather(
        database_ready(),
        redis_ready(),
        manager_tls_ready(),
    )
    return {
        "database": database,
        "redis": redis,
        "manager_tls": manager_tls,
    }


async def render_prometheus_metrics(
    *,
    redis_is_ready: bool | None = None,
    tls_status: ManagerTLSStatus | None = None,
    now: datetime | None = None,
) -> str:
    """Render low-cardinality operational state directly from PostgreSQL."""

    started = time.monotonic()
    current_time = _as_utc(now or datetime.now(timezone.utc))
    if redis_is_ready is None:
        redis_is_ready = await redis_ready()
    if tls_status is None:
        tls_status = inspect_manager_tls_material(now=current_time)

    async with db.async_session() as session:
        billing_rows = (
            await session.execute(
                select(BillingRun.status, func.count(BillingRun.id)).group_by(
                    BillingRun.status
                )
            )
        ).all()
        last_billing = (
            await session.execute(
                select(
                    BillingRun.completed_at,
                    BillingRun.charged_users,
                    BillingRun.total_amount,
                )
                .where(BillingRun.status == "completed")
                .order_by(BillingRun.completed_at.desc())
                .limit(1)
            )
        ).one_or_none()
        oldest_running_billing = await session.scalar(
            select(func.min(BillingRun.created_at)).where(
                BillingRun.status == "running"
            )
        )

        vpn_rows = (
            await session.execute(
                select(VPNOperation.status, func.count(VPNOperation.id)).group_by(
                    VPNOperation.status
                )
            )
        ).all()
        oldest_vpn_backlog = await session.scalar(
            select(func.min(VPNOperation.created_at)).where(
                VPNOperation.status.in_(_VPN_BACKLOG_STATUSES)
            )
        )

        outbox_rows = (
            await session.execute(
                select(
                    NotificationOutbox.status,
                    func.count(NotificationOutbox.id),
                ).group_by(NotificationOutbox.status)
            )
        ).all()
        oldest_pending_outbox = await session.scalar(
            select(func.min(NotificationOutbox.created_at)).where(
                NotificationOutbox.status == "pending"
            )
        )
        oldest_outbox_backlog = await session.scalar(
            select(
                func.min(
                    case(
                        (
                            NotificationOutbox.status == "queued",
                            func.coalesce(
                                NotificationOutbox.published_at,
                                NotificationOutbox.created_at,
                            ),
                        ),
                        else_=NotificationOutbox.created_at,
                    )
                )
            ).where(NotificationOutbox.status.in_(_OUTBOX_BACKLOG_STATUSES))
        )
        retrying_outbox = await session.scalar(
            select(func.count(NotificationOutbox.id)).where(
                NotificationOutbox.status == "pending",
                NotificationOutbox.attempts > 0,
            )
        )
        outbox_attempts = await session.scalar(
            select(func.coalesce(func.sum(NotificationOutbox.attempts), 0))
        )

        telegram_rows = (
            await session.execute(
                select(
                    TelegramUpdateInbox.status,
                    func.count(TelegramUpdateInbox.id),
                ).group_by(TelegramUpdateInbox.status)
            )
        ).all()
        oldest_telegram_backlog = await session.scalar(
            select(func.min(TelegramUpdateInbox.received_at)).where(
                TelegramUpdateInbox.status.in_(_TELEGRAM_BACKLOG_STATUSES)
            )
        )

        fleet_servers = (
            await session.scalars(select(Server).order_by(Server.id))
        ).all()
        fleet_status_rows = (
            await session.scalars(
                select(VPNServerStatus)
                .where(VPNServerStatus.kind == "status")
                .order_by(
                    VPNServerStatus.server_id,
                    VPNServerStatus.collected_at.desc(),
                    VPNServerStatus.id.desc(),
                )
            )
        ).all()
        fleet_config_rows = (
            await session.execute(
                select(VPN_Config.server_id, func.count(VPN_Config.id))
                .where(VPN_Config.desired_state != VPNState.REVOKED.value)
                .group_by(VPN_Config.server_id)
            )
        ).all()

    billing_counts = _bounded_counts(billing_rows, _BILLING_STATUSES)
    vpn_counts = _bounded_counts(
        vpn_rows, tuple(status.value for status in VPNOperationStatus)
    )
    outbox_counts = _bounded_counts(outbox_rows, _OUTBOX_STATUSES)
    telegram_counts = _bounded_counts(
        telegram_rows,
        tuple(status.value for status in TelegramUpdateStatus),
    )
    latest_status_by_server: dict[int, VPNServerStatus] = {}
    for row in fleet_status_rows:
        latest_status_by_server.setdefault(row.server_id, row)
    managed_configs = {
        int(server_id): int(count) for server_id, count in fleet_config_rows
    }
    fleet_lifecycle = {state: 0 for state in _FLEET_LIFECYCLE_STATES}
    fleet_health = {state: 0 for state in _FLEET_HEALTH_STATES}
    fleet_missing_status = 0
    fleet_online_sessions = 0
    fleet_capacity_servers = 0
    fleet_capacity_total = 0
    fleet_capacity_available = 0
    fleet_servers_at_capacity = 0
    latest_status_times: list[datetime] = []
    certificate_expiries: list[datetime] = []
    for server in fleet_servers:
        lifecycle = str(server.lifecycle_state)
        fleet_lifecycle[lifecycle if lifecycle in fleet_lifecycle else "disabled"] += 1
        latest = latest_status_by_server.get(server.id)
        health = _fleet_health(server, latest, current_time)
        fleet_health[health] += 1
        if latest is None:
            if lifecycle in {
                ServerLifecycleState.ACTIVE.value,
                ServerLifecycleState.DRAINING.value,
            }:
                fleet_missing_status += 1
        else:
            latest_status_times.append(_as_utc(latest.collected_at))
            if latest.success and isinstance(latest.snapshot, dict):
                plane = latest.snapshot.get("data_plane", {})
                if isinstance(plane, dict):
                    sessions = plane.get("online_sessions")
                    if (
                        isinstance(sessions, int)
                        and not isinstance(sessions, bool)
                        and sessions >= 0
                    ):
                        fleet_online_sessions += sessions
                pki = latest.snapshot.get("pki", {})
                certificate = (
                    pki.get("server_certificate", {}) if isinstance(pki, dict) else {}
                )
                expires_at = (
                    certificate.get("expires_at")
                    if isinstance(certificate, dict)
                    else None
                )
                parsed_expiry = _parse_timestamp(expires_at)
                if (
                    parsed_expiry is not None
                    and lifecycle != ServerLifecycleState.RETIRED.value
                ):
                    certificate_expiries.append(parsed_expiry)
        if server.max_configs is not None:
            fleet_capacity_servers += 1
            fleet_capacity_total += server.max_configs
            available = max(
                0,
                server.max_configs
                - server.capacity_reserve
                - managed_configs.get(server.id, 0),
            )
            fleet_capacity_available += available
            if (
                lifecycle == ServerLifecycleState.ACTIVE.value
                and server.accepts_new_configs
                and available == 0
            ):
                fleet_servers_at_capacity += 1

    lines: list[str] = []
    _family(
        lines,
        "vpn_hub_info",
        "Static VPN Hub process information.",
        "gauge",
        [({"service": settings.vpn_hub_service}, 1)],
    )
    _family(
        lines,
        "vpn_hub_observability_start_timestamp_seconds",
        "Unix timestamp when this metrics process started.",
        "gauge",
        [({}, _PROCESS_START_TIMESTAMP_SECONDS)],
    )
    _family(
        lines,
        "vpn_hub_dependency_ready",
        "Whether a required dependency responded to a readiness probe.",
        "gauge",
        [
            ({"dependency": "database"}, 1),
            ({"dependency": "redis"}, int(redis_is_ready)),
            ({"dependency": "manager_tls"}, int(tls_status.ready)),
        ],
    )
    _family(
        lines,
        "vpn_hub_manager_tls_enabled",
        "Whether Manager HTTPS is enabled for this process.",
        "gauge",
        [({}, int(tls_status.enabled))],
    )
    _family(
        lines,
        "vpn_hub_manager_tls_material_ready",
        "Whether configured Manager TLS files are readable, current, and consistent.",
        "gauge",
        [({}, int(tls_status.ready))],
    )
    _family(
        lines,
        "vpn_hub_manager_tls_certificate_expiry_timestamp_seconds",
        "Unix expiry timestamp for configured Manager TLS certificates.",
        "gauge",
        [
            ({"certificate": certificate}, _timestamp(not_after))
            for certificate, not_after in tls_status.certificate_not_after
        ],
    )
    _family(
        lines,
        "vpn_hub_feature_enabled",
        "Current state of operational feature switches loaded by this process.",
        "gauge",
        [
            ({"feature": "billing"}, int(settings.billing_enabled)),
            ({"feature": "payments"}, int(settings.payments_enabled)),
            ({"feature": "provisioning"}, int(settings.provisioning_enabled)),
            ({"feature": "notifications"}, int(settings.notifications_enabled)),
            ({"feature": "maintenance"}, int(settings.maintenance_mode)),
        ],
    )
    _family(
        lines,
        "vpn_hub_fleet_servers",
        "Managed VPN servers grouped by lifecycle state.",
        "gauge",
        [({"lifecycle": state}, count) for state, count in fleet_lifecycle.items()],
    )
    _family(
        lines,
        "vpn_hub_fleet_server_health",
        "Managed VPN servers grouped by their latest bounded health state.",
        "gauge",
        [({"status": state}, count) for state, count in fleet_health.items()],
    )
    _family(
        lines,
        "vpn_hub_fleet_servers_missing_status",
        "Managed VPN servers without a persisted Manager status snapshot.",
        "gauge",
        [({}, fleet_missing_status)],
    )
    _family(
        lines,
        "vpn_hub_fleet_oldest_status_age_seconds",
        "Age of the oldest latest-per-server Manager status snapshot.",
        "gauge",
        [
            (
                {},
                (
                    _age_seconds(min(latest_status_times), current_time)
                    if latest_status_times
                    else 0
                ),
            )
        ],
    )
    _family(
        lines,
        "vpn_hub_fleet_online_sessions",
        "Aggregate online sessions from successful bounded Manager snapshots.",
        "gauge",
        [({}, fleet_online_sessions)],
    )
    _family(
        lines,
        "vpn_hub_fleet_capacity_configured_servers",
        "Managed servers with an explicit configuration capacity.",
        "gauge",
        [({}, fleet_capacity_servers)],
    )
    _family(
        lines,
        "vpn_hub_fleet_capacity_total",
        "Aggregate configured maximum VPN profile capacity.",
        "gauge",
        [({}, fleet_capacity_total)],
    )
    _family(
        lines,
        "vpn_hub_fleet_capacity_available",
        "Aggregate remaining VPN profile capacity after reserves.",
        "gauge",
        [({}, fleet_capacity_available)],
    )
    _family(
        lines,
        "vpn_hub_fleet_servers_at_capacity",
        "Active placement-enabled servers with no remaining configured capacity.",
        "gauge",
        [({}, fleet_servers_at_capacity)],
    )
    _family(
        lines,
        "vpn_hub_fleet_server_certificate_earliest_expiry_timestamp_seconds",
        "Earliest server-certificate expiry in successful Manager snapshots.",
        "gauge",
        [({}, _timestamp(min(certificate_expiries)) if certificate_expiries else 0)],
    )
    _family(
        lines,
        "vpn_hub_billing_interval_seconds",
        "Configured periodic billing interval.",
        "gauge",
        [({}, settings.billing_interval)],
    )
    _family(
        lines,
        "vpn_hub_billing_runs",
        "Persisted billing runs grouped by status.",
        "gauge",
        [({"status": status}, count) for status, count in billing_counts.items()],
    )
    _family(
        lines,
        "vpn_hub_billing_last_completed_timestamp_seconds",
        "Unix timestamp of the latest completed billing run, or zero.",
        "gauge",
        [({}, _timestamp(last_billing[0]) if last_billing else 0)],
    )
    _family(
        lines,
        "vpn_hub_billing_last_charged_users",
        "Users charged by the latest completed billing run.",
        "gauge",
        [({}, last_billing[1] if last_billing else 0)],
    )
    _family(
        lines,
        "vpn_hub_billing_last_amount_rubles",
        "Total amount charged by the latest completed billing run.",
        "gauge",
        [({}, last_billing[2] if last_billing else Decimal("0.00"))],
    )
    _family(
        lines,
        "vpn_hub_billing_oldest_running_age_seconds",
        "Age of the oldest non-completed billing run.",
        "gauge",
        [({}, _age_seconds(oldest_running_billing, current_time))],
    )
    _family(
        lines,
        "vpn_hub_vpn_operations",
        "Durable VPN lifecycle operations grouped by status.",
        "gauge",
        [({"status": status}, count) for status, count in vpn_counts.items()],
    )
    _family(
        lines,
        "vpn_hub_vpn_operation_backlog",
        "VPN operations that are pending, running, or retryable failures.",
        "gauge",
        [({}, sum(vpn_counts[status] for status in _VPN_BACKLOG_STATUSES))],
    )
    _family(
        lines,
        "vpn_hub_vpn_operation_oldest_backlog_age_seconds",
        "Age of the oldest operation in the retryable backlog.",
        "gauge",
        [({}, _age_seconds(oldest_vpn_backlog, current_time))],
    )
    _family(
        lines,
        "vpn_hub_notification_outbox",
        "Billing notification outbox rows grouped by status.",
        "gauge",
        [({"status": status}, count) for status, count in outbox_counts.items()],
    )
    _family(
        lines,
        "vpn_hub_notification_outbox_oldest_pending_age_seconds",
        "Age of the oldest unpublished notification outbox row.",
        "gauge",
        [({}, _age_seconds(oldest_pending_outbox, current_time))],
    )
    _family(
        lines,
        "vpn_hub_notification_outbox_backlog",
        "Notification outbox rows pending publication or awaiting delivery visibility.",
        "gauge",
        [({}, sum(outbox_counts[status] for status in _OUTBOX_BACKLOG_STATUSES))],
    )
    _family(
        lines,
        "vpn_hub_notification_outbox_oldest_backlog_age_seconds",
        "Age of the oldest pending row or queued visibility timestamp.",
        "gauge",
        [({}, _age_seconds(oldest_outbox_backlog, current_time))],
    )
    _family(
        lines,
        "vpn_hub_notification_outbox_visibility_timeout_seconds",
        "Configured visibility timeout for queued notification delivery.",
        "gauge",
        [({}, settings.notification_visibility_timeout)],
    )
    _family(
        lines,
        "vpn_hub_notification_outbox_retrying",
        "Pending outbox rows that have failed at least once.",
        "gauge",
        [({}, retrying_outbox or 0)],
    )
    _family(
        lines,
        "vpn_hub_notification_outbox_attempts",
        "Cumulative publish attempts retained in the notification outbox.",
        "gauge",
        [({}, outbox_attempts or 0)],
    )
    _family(
        lines,
        "vpn_hub_telegram_update_inbox",
        "Durable Telegram inbox rows grouped by bounded lifecycle status.",
        "gauge",
        [({"status": status}, count) for status, count in telegram_counts.items()],
    )
    _family(
        lines,
        "vpn_hub_telegram_update_inbox_backlog",
        "Telegram updates waiting, processing, or eligible for retry.",
        "gauge",
        [
            (
                {},
                sum(telegram_counts[status] for status in _TELEGRAM_BACKLOG_STATUSES),
            )
        ],
    )
    _family(
        lines,
        "vpn_hub_telegram_update_inbox_oldest_backlog_age_seconds",
        "Age of the oldest Telegram update not yet terminally handled.",
        "gauge",
        [({}, _age_seconds(oldest_telegram_backlog, current_time))],
    )
    _family(
        lines,
        "vpn_hub_telegram_update_inbox_dead",
        "Telegram updates that exhausted their processing budget.",
        "gauge",
        [({}, telegram_counts[TelegramUpdateStatus.DEAD.value])],
    )
    _family(
        lines,
        "vpn_hub_metrics_collection_duration_seconds",
        "Time spent building the database-backed metrics snapshot.",
        "gauge",
        [({}, time.monotonic() - started)],
    )
    return "\n".join(lines) + "\n"


def _bounded_counts(
    rows: Iterable[Sequence[object]], expected: Sequence[str]
) -> dict[str, int]:
    counts = {status: 0 for status in expected}
    counts["unknown"] = 0
    for status, count in rows:
        key = str(status)
        if key not in counts:
            key = "unknown"
        counts[key] += int(count)
    return counts


def _family(
    lines: list[str],
    name: str,
    help_text: str,
    metric_type: str,
    samples: Iterable[tuple[dict[str, object], object]],
) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {metric_type}")
    for labels, value in samples:
        rendered_labels = ""
        if labels:
            rendered_labels = (
                "{"
                + ",".join(
                    f'{key}="{_escape_label(label_value)}"'
                    for key, label_value in sorted(labels.items())
                )
                + "}"
            )
        lines.append(f"{name}{rendered_labels} {_number(value)}")


def _number(value: object) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return format(float(value), ".12g")


def _escape_label(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def _fleet_health(
    server: Server,
    latest: VPNServerStatus | None,
    now: datetime,
) -> str:
    if server.lifecycle_state in {
        ServerLifecycleState.DISABLED.value,
        ServerLifecycleState.RETIRED.value,
    }:
        return server.lifecycle_state
    if latest is None:
        return "unknown"
    if not latest.success:
        return "unreachable"
    if (
        _age_seconds(latest.collected_at, now)
        > settings.admin_fleet_status_stale_seconds
    ):
        return "stale"
    if (
        latest.manager_instance_id
        and server.manager_instance_id
        and latest.manager_instance_id != server.manager_instance_id
    ):
        return "instance_mismatch"
    snapshot = latest.snapshot if isinstance(latest.snapshot, dict) else {}
    readiness = snapshot.get("readiness", {})
    data_plane = snapshot.get("data_plane", {})
    if not isinstance(readiness, dict) or readiness.get("ready") is not True:
        return "unhealthy"
    if not isinstance(data_plane, dict) or data_plane.get("status") != "up":
        return "unhealthy"
    return "healthy"


def _timestamp(value: datetime | None) -> float:
    return _as_utc(value).timestamp() if value is not None else 0.0


def _age_seconds(value: datetime | None, now: datetime) -> float:
    if value is None:
        return 0.0
    return max(0.0, (now - _as_utc(value)).total_seconds())
