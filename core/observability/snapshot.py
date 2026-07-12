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
from core.db.models.notification_outbox import NotificationOutbox
from core.db.models.telegram_update import TelegramUpdateInbox
from core.db.models.vpn_operation import VPNOperation
from core.db.schema import EXPECTED_ALEMBIC_REVISION
from core.domain import TelegramUpdateStatus, VPNOperationStatus

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

    billing_counts = _bounded_counts(billing_rows, _BILLING_STATUSES)
    vpn_counts = _bounded_counts(
        vpn_rows, tuple(status.value for status in VPNOperationStatus)
    )
    outbox_counts = _bounded_counts(outbox_rows, _OUTBOX_STATUSES)
    telegram_counts = _bounded_counts(
        telegram_rows,
        tuple(status.value for status in TelegramUpdateStatus),
    )

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
            ({"feature": "provisioning"}, int(settings.provisioning_enabled)),
            ({"feature": "notifications"}, int(settings.notifications_enabled)),
            ({"feature": "maintenance"}, int(settings.maintenance_mode)),
        ],
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


def _timestamp(value: datetime | None) -> float:
    return _as_utc(value).timestamp() if value is not None else 0.0


def _age_seconds(value: datetime | None, now: datetime) -> float:
    if value is None:
        return 0.0
    return max(0.0, (now - _as_utc(value)).total_seconds())
