from fastapi import APIRouter, Response, status
from fastapi.responses import JSONResponse

from core.config import settings
from core.observability.snapshot import (
    PROMETHEUS_CONTENT_TYPE,
    dependency_readiness,
    render_prometheus_metrics,
)

router = APIRouter(tags=["observability"])


@router.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """Liveness only: a running event loop can answer without dependencies."""

    return {"status": "ok", "service": settings.vpn_hub_service}


@router.get("/ready", include_in_schema=False)
async def ready() -> JSONResponse:
    """Readiness requires PostgreSQL, Redis, and configured Manager TLS files."""

    dependencies = await dependency_readiness()
    ready_now = all(dependencies.values())
    return JSONResponse(
        status_code=(
            status.HTTP_200_OK if ready_now else status.HTTP_503_SERVICE_UNAVAILABLE
        ),
        content={
            "status": "ready" if ready_now else "not_ready",
            "dependencies": dependencies,
            "maintenance_mode": settings.maintenance_mode,
        },
    )


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Prometheus text exposition with no user, config, or server labels."""

    dependencies = await dependency_readiness()
    payload = await render_prometheus_metrics(
        redis_is_ready=dependencies["redis"],
    )
    return Response(content=payload, media_type=PROMETHEUS_CONTENT_TYPE)
