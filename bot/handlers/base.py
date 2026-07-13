from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.types import BotCommand, BotCommandScopeDefault

from core.config import settings
from core.db.unit_of_work import uow
from core.services import BillingService, ConfigService, ServerService, UserService

router = Router()
router.message.filter(F.chat.type == ChatType.PRIVATE)
router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

user_service = UserService(uow)
server_service = ServerService(uow)
config_service = ConfigService(uow)
billing_service = BillingService(uow, per_config_cost=settings.per_config_cost)

AVAILABLE_AMOUNTS = [100, 200, 300, 500]


async def get_or_create_user(
    tg_id: int,
    username: str | None,
    ref_id: str | None = None,
):
    return await user_service.register_invited(
        tg_id,
        username=username,
        referral_code=ref_id,
    )


async def setup_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="Открыть главное меню"),
        BotCommand(command="menu", description="Показать кнопки меню"),
        BotCommand(command="help", description="Помощь"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
