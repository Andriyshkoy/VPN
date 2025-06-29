"""Telegram bot handlers package."""

from aiogram.types import FSInputFile

from .base import (
    router,
    setup_bot_commands,
    billing_service,
    config_service,
)

# Import modules so handlers are registered
from . import common  # noqa: F401
from . import referrals  # noqa: F401
from . import payments  # noqa: F401
from . import configs  # noqa: F401

# Re-export frequently used callables for tests
from .configs import (
    got_name,
    download_config_cb,
    rename_config_cb,
    got_new_name,
    show_config,
)
from .base import get_or_create_user, server_service
from core.exceptions import ServiceError
from ..states import RenameConfig

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
