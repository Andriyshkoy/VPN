from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.db.models import VPN_Config, VPNServerStatus
from core.domain import ServerLifecycleState, VPNState


@dataclass(frozen=True, slots=True)
class PlacementDecision:
    """A fail-closed placement decision with a stable machine-readable reason."""

    allowed: bool
    reason: str | None = None


def managed_config_condition():
    """Return the SQL predicate for configs that may still exist on Manager.

    A revoke intent must keep consuming capacity and must keep endpoint/retire
    mutations fenced until Manager has actually converged to ``revoked``.
    """

    return or_(
        VPN_Config.desired_state != VPNState.REVOKED.value,
        VPN_Config.actual_state != VPNState.REVOKED.value,
    )


def is_managed_config(config: object) -> bool:
    """Mirror :func:`managed_config_condition` for already-loaded rows."""

    return not (
        getattr(config, "desired_state", None) == VPNState.REVOKED.value
        and getattr(config, "actual_state", None) == VPNState.REVOKED.value
    )


async def latest_server_status(
    session: AsyncSession,
    server_id: int,
) -> VPNServerStatus | None:
    """Load the newest Manager health observation for placement decisions."""

    return await session.scalar(
        select(VPNServerStatus)
        .where(
            VPNServerStatus.server_id == server_id,
            VPNServerStatus.kind == "status",
        )
        .order_by(VPNServerStatus.collected_at.desc(), VPNServerStatus.id.desc())
        .limit(1)
    )


def manager_readiness_decision(
    server: object,
    latest: VPNServerStatus | None,
    *,
    now: datetime | None = None,
    stale_seconds: int | None = None,
) -> PlacementDecision:
    """Validate Manager reachability, freshness, identity and data-plane health."""

    if latest is None or not latest.success:
        return PlacementDecision(False, "health_check_required")

    checked_at = latest.collected_at
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    maximum_age = (
        settings.admin_fleet_status_stale_seconds
        if stale_seconds is None
        else stale_seconds
    )
    age_seconds = (
        current.astimezone(timezone.utc) - checked_at.astimezone(timezone.utc)
    ).total_seconds()
    if age_seconds > maximum_age:
        return PlacementDecision(False, "health_check_required")

    expected_instance = getattr(server, "manager_instance_id", None)
    if (
        not expected_instance
        or not latest.manager_instance_id
        or expected_instance != latest.manager_instance_id
    ):
        return PlacementDecision(False, "manager_identity_mismatch")

    snapshot = latest.snapshot if isinstance(latest.snapshot, dict) else {}
    readiness = snapshot.get("readiness")
    data_plane = snapshot.get("data_plane")
    if (
        not isinstance(readiness, dict)
        or readiness.get("ready") is not True
        or not isinstance(data_plane, dict)
        or data_plane.get("status") != "up"
    ):
        return PlacementDecision(False, "manager_unhealthy")

    return PlacementDecision(True)


def placement_decision(
    server: object,
    managed_configs: int,
    latest: VPNServerStatus | None,
    *,
    now: datetime | None = None,
    stale_seconds: int | None = None,
) -> PlacementDecision:
    """Decide whether a locked server may receive one more configuration."""

    if (
        getattr(server, "lifecycle_state", None) != ServerLifecycleState.ACTIVE.value
        or getattr(server, "accepts_new_configs", False) is not True
    ):
        return PlacementDecision(False, "placement_disabled")

    maximum = getattr(server, "max_configs", None)
    reserve = int(getattr(server, "capacity_reserve", 0) or 0)
    if maximum is not None and managed_configs >= maximum - reserve:
        return PlacementDecision(False, "capacity_exhausted")

    return manager_readiness_decision(
        server,
        latest,
        now=now,
        stale_seconds=stale_seconds,
    )


__all__ = [
    "PlacementDecision",
    "is_managed_config",
    "latest_server_status",
    "managed_config_condition",
    "manager_readiness_decision",
    "placement_decision",
]
