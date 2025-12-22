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
billing_service = BillingService(uow)

REFERRALS_PER_PAGE = 10
AVAILABLE_AMOUNTS = [100, 200, 300, 500]
ADMIN_TG_IDS = {
    int(item)
    for item in (settings.admin_tg_ids or "").split(",")
    if item.strip().isdigit()
}


def is_admin(tg_id: int) -> bool:
    return tg_id in ADMIN_TG_IDS


async def get_or_create_user(
    tg_id: int,
    username: str,
    ref_id: str | None = None,
):
    existing = await user_service.get_by_tg_id(tg_id)
    if existing:
        if username and username != existing.username:
            return await user_service.register(tg_id, username=username)
        return existing

    if is_admin(tg_id):
        return await user_service.register(tg_id, username=username)

    if not ref_id or not ref_id.isdigit():
        return None
    ref_tg_id = int(ref_id)
    ref_user = await user_service.get_by_tg_id(ref_tg_id)
    if not ref_user:
        return None
    return await user_service.register(tg_id, username=username, ref_id=ref_tg_id)


async def require_user(target) -> object | None:
    tg_id = target.from_user.id
    username = target.from_user.username
    user = await get_or_create_user(tg_id, username)
    if user:
        return user
    text = (
        "Регистрация доступна только по валидной реферальной ссылке.\n"
        "Попросите приглашение у действующего пользователя."
    )
    if hasattr(target, "message"):
        await target.message.answer(text)
        await target.answer()
    else:
        await target.answer(text)
    return None


async def setup_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="Начать работу с ботом"),
        BotCommand(command="help", description="Показать справку"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
