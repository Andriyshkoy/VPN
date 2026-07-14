from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from core.db.unit_of_work import uow
from core.services.admin_queries import AdminAnalyticsQueryService

from ..fleet_service import AdminFleetService
from ..security import AdminPermission, AdminPrincipal, require_permission

router = APIRouter(prefix="/api/admin/v1", tags=["admin-v1-analytics"])
analytics = AdminAnalyticsQueryService(uow)
fleet = AdminFleetService()

DashboardRead = Annotated[
    AdminPrincipal,
    Depends(require_permission(AdminPermission.DASHBOARD_READ)),
]
FinanceRead = Annotated[
    AdminPrincipal,
    Depends(require_permission(AdminPermission.FINANCE_READ)),
]


def _period(
    period_from: datetime | None,
    period_to: datetime | None,
) -> tuple[datetime, datetime]:
    end = period_to or datetime.now(timezone.utc)
    start = period_from or (end - timedelta(days=30))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    else:
        start = start.astimezone(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    else:
        end = end.astimezone(timezone.utc)
    if end <= start or end - start > timedelta(days=730):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Analytics period must be positive and no longer than 730 days",
        )
    return start, end


@router.get("/dashboard")
async def dashboard(
    principal: DashboardRead,
    period_from: datetime | None = Query(default=None, alias="from"),
    period_to: datetime | None = Query(default=None, alias="to"),
):
    start, end = _period(period_from, period_to)
    payload = await analytics.dashboard(period_from=start, period_to=end)
    if AdminPermission.FINANCE_READ not in principal.permissions:
        payload.pop("finance", None)
        payload.pop("billing", None)
        payload.get("users", {}).pop("paying", None)
    if AdminPermission.REFERRALS_READ not in principal.permissions:
        payload.get("users", {}).pop("invited", None)
    if AdminPermission.SERVERS_READ not in principal.permissions:
        payload.pop("servers", None)
    else:
        fleet_page = await fleet.list_servers(limit=100, offset=0)
        if AdminPermission.FINANCE_READ not in principal.permissions:
            for item in fleet_page["items"]:
                item.pop("monthly_cost", None)
        payload["servers"] = fleet_page["items"]
        payload["server_total"] = fleet_page["total"]
    if not {
        AdminPermission.CONFIGS_READ,
        AdminPermission.SERVERS_READ,
    }.intersection(principal.permissions):
        payload.pop("operations", None)
    return payload


@router.get("/analytics/overview")
async def analytics_overview(
    _principal: FinanceRead,
    period_from: datetime | None = Query(default=None, alias="from"),
    period_to: datetime | None = Query(default=None, alias="to"),
):
    start, end = _period(period_from, period_to)
    return await analytics.overview(period_from=start, period_to=end)


@router.get("/analytics/finance/timeseries")
async def finance_timeseries(
    _principal: FinanceRead,
    period_from: datetime | None = Query(default=None, alias="from"),
    period_to: datetime | None = Query(default=None, alias="to"),
    granularity: str = Query(default="day", pattern="^(day|week|month)$"),
    timezone_name: str = Query(default="UTC", alias="timezone", max_length=64),
):
    start, end = _period(period_from, period_to)
    try:
        items = await analytics.finance_timeseries(
            period_from=start,
            period_to=end,
            granularity=granularity,
            timezone_name=timezone_name,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return {
        "items": items,
        "period": {"from": start.isoformat(), "to": end.isoformat()},
        "granularity": granularity,
        "timezone": timezone_name,
    }
