import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from core.config import settings
from core.db.unit_of_work import uow
from core.services import BillingService, ConfigService, ServerService, UserService

from . import exception_handlers
from .dependencies import parse  # noqa: F401 - historical public import
from .request_context import RequestContextMiddleware
from .routers import admin_v1_analytics as admin_v1_analytics_router
from .routers import admin_v1_configs as admin_v1_configs_router
from .routers import admin_v1_finance as admin_v1_finance_router
from .routers import admin_v1_fleet as admin_v1_fleet_router
from .routers import admin_v1_referrals as admin_v1_referrals_router
from .routers import admin_v1_system as admin_v1_system_router
from .routers import admin_v1_users as admin_v1_users_router
from .routers import auth as auth_router
from .routers import auth_v1 as auth_v1_router
from .routers import configs as config_router
from .routers import observability as observability_router
from .routers import servers as server_router
from .routers import users as user_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Recover abandoned actions and maintain bounded fleet observations."""

    await admin_v1_fleet_router.fleet.recover_stale_actions()
    stop = asyncio.Event()
    poller = None
    if settings.admin_fleet_poll_enabled:
        poller = asyncio.create_task(
            admin_v1_fleet_router.fleet.run_status_poller(stop),
            name="admin-fleet-status-poller",
        )
    try:
        yield
    finally:
        stop.set()
        if poller is not None:
            poller.cancel()
            with suppress(asyncio.CancelledError):
                await poller


app = FastAPI(lifespan=lifespan)

server_service = ServerService(uow)
config_service = ConfigService(uow)
user_service = UserService(uow)
billing_service = BillingService(uow, per_config_cost=settings.per_config_cost)

# The admin SPA is same-origin in production and local Docker. Vite development
# uses its own `/api` proxy, so credentialed cross-origin access is unnecessary.
# Keeping CORS disabled prevents another same-site product origin from reading
# sensitive admin GET responses with a Strict session cookie.
app.add_middleware(RequestContextMiddleware)

# Register exception handlers
exception_handlers.register_exception_handlers(app)

# Include the versioned, session-based control plane. The pre-v2 bearer API is
# available only behind an explicit rollback switch and is off by default.
app.include_router(auth_v1_router.router)
app.include_router(admin_v1_fleet_router.router)
app.include_router(admin_v1_fleet_router.operations_router)
app.include_router(admin_v1_analytics_router.router)
app.include_router(admin_v1_users_router.router)
app.include_router(admin_v1_configs_router.router)
app.include_router(admin_v1_finance_router.router)
app.include_router(admin_v1_referrals_router.router)
app.include_router(admin_v1_system_router.router)
if settings.admin_legacy_api_enabled:
    app.include_router(auth_router.router)
    app.include_router(server_router.router)
    app.include_router(user_router.router)
    app.include_router(config_router.router)
app.include_router(observability_router.router)
