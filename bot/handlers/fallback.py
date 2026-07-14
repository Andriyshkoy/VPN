from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import Message

from core.services import TelegramActionAuditContext

from ..keyboards import main_menu_keyboard


async def unknown_text(
    message: Message,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    await message.answer(
        "Не понял сообщение. Выберите действие на клавиатуре 👇",
        reply_markup=main_menu_keyboard(),
    )
    if telegram_action_audit is not None:
        telegram_action_audit.record(
            "message.unrecognized",
            result="invalid",
            metadata={
                "content_type": "text",
                "reason_code": "unsupported_input",
            },
        )


def register(router: Router) -> None:
    router.message(StateFilter(None), F.text)(unknown_text)
