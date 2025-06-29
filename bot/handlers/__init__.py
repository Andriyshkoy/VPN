"""Telegram bot handlers package."""

from aiogram.types import FSInputFile

from core.exceptions import ServiceError

from ..states import RenameConfig

# Import modules so handlers are registered
from . import common  # noqa: F401
from . import configs  # noqa: F401
from . import payments  # noqa: F401
from . import referrals  # noqa: F401
from .base import (
    billing_service,
    config_service,
    get_or_create_user,
    router,
    server_service,
    setup_bot_commands,
)

# Re-export frequently used callables for tests
from .configs import (
    download_config_cb,
    got_name,
    got_new_name,
    rename_config_cb,
    show_config,
)

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
