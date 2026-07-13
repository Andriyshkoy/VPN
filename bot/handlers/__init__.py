"""Telegram bot handlers package."""

from aiogram import Router
from aiogram.types import FSInputFile

from core.exceptions import ServiceError

from ..middlewares import InviteOnlyAccessMiddleware
from ..states import RenameConfig

# Import modules so feature handlers are registered on the base router.
from . import balance_history  # noqa: F401
from . import common  # noqa: F401
from . import configs  # noqa: F401
from . import payments  # noqa: F401
from . import referrals  # noqa: F401
from . import fallback
from .base import billing_service, config_service, get_or_create_user
from .base import router as feature_router
from .base import server_service, setup_bot_commands, user_service

# Re-export frequently used callables for tests.
from .configs import (
    download_config_cb,
    got_name,
    got_new_name,
    rename_config_cb,
    show_config,
)
from .navigation import router as navigation_router
from .payments import payment_events_router
from .privacy import router as privacy_router

# Captured payments run before privacy and every FSM catch-all. The privacy
# boundary then rejects other group access; navigation precedes feature text
# handlers so a menu label cannot become a configuration name accidentally.
fallback.register(feature_router)
router = Router(name="telegram-bot")
invite_access_middleware = InviteOnlyAccessMiddleware(user_service)
router.message.outer_middleware(invite_access_middleware)
router.callback_query.outer_middleware(invite_access_middleware)
router.pre_checkout_query.outer_middleware(invite_access_middleware)
router.include_router(payment_events_router)
router.include_router(privacy_router)
router.include_router(navigation_router)
router.include_router(feature_router)

__all__ = [
    "router",
    "setup_bot_commands",
    "got_name",
    "download_config_cb",
    "rename_config_cb",
    "got_new_name",
    "show_config",
    "FSInputFile",
    "billing_service",
    "config_service",
    "get_or_create_user",
    "server_service",
    "RenameConfig",
    "ServiceError",
]
