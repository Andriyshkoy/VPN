from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message

from ..ui import safe_callback_answer

router = Router(name="telegram-private-chat-boundary")


@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def group_message(message: Message) -> None:
    await message.answer(
        "🔐 Из соображений безопасности баланс и VPN-конфигурации доступны "
        "только в личном чате. Откройте профиль бота и нажмите «Начать»."
    )


@router.callback_query(F.message.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def group_callback(callback: CallbackQuery) -> None:
    await safe_callback_answer(
        callback,
        "Откройте бота в личном чате.",
        show_alert=True,
    )
