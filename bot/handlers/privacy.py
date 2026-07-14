from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message

from core.services import TelegramActionAuditContext

from ..ui import safe_callback_answer

router = Router(name="telegram-private-chat-boundary")


@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def group_message(
    message: Message,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    await message.answer(
        "🔐 Из соображений безопасности баланс и VPN-конфигурации доступны "
        "только в личном чате. Откройте профиль бота и нажмите «Начать»."
    )
    if telegram_action_audit is not None:
        telegram_action_audit.record(
            "privacy.non_private_input",
            result="rejected",
            metadata={"reason_code": "private_chat_required"},
        )


@router.callback_query(F.message.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def group_callback(
    callback: CallbackQuery,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    await safe_callback_answer(
        callback,
        "Откройте бота в личном чате.",
        show_alert=True,
    )
    if telegram_action_audit is not None:
        telegram_action_audit.record(
            "privacy.non_private_input",
            result="rejected",
            metadata={"reason_code": "private_chat_required"},
        )
