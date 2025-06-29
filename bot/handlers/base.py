from __future__ import annotations

from aiogram import Bot, Router
from aiogram.types import BotCommand, BotCommandScopeDefault

from core.config import settings
from core.db.unit_of_work import uow
from core.services import BillingService, ConfigService, ServerService, UserService

router = Router()

user_service = UserService(uow)
server_service = ServerService(uow)
config_service = ConfigService(uow)
billing_service = BillingService(uow, per_config_cost=settings.per_config_cost)

REFERRALS_PER_PAGE = 10
AVAILABLE_AMOUNTS = [100, 200, 300, 500]


async def get_or_create_user(tg_id: int, username: str, ref_id: str | None = None):
    ref_id = int(ref_id) if ref_id and ref_id.isdigit() else None
    return await user_service.register(tg_id, username=username, ref_id=ref_id)


async def setup_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="Начать работу с ботом"),
        BotCommand(command="help", description="Показать справку"),
        BotCommand(command="topup", description="Пополнить баланс"),
        BotCommand(command="how_to_use", description="Как установить VPN"),
        BotCommand(command="balance", description="Проверить баланс"),
        BotCommand(command="configs", description="Список конфигураций"),
        BotCommand(command="create_config", description="Создать новую конфигурацию"),
        BotCommand(command="referrals", description="Реферальная программа"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
