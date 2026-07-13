from __future__ import annotations

from aiogram import F
from aiogram.types import CallbackQuery, Message

from ..keyboards import main_menu_keyboard
from ..ui import safe_callback_answer, safe_edit_text
from .base import router

__all__ = ["cmd_referrals", "legacy_referrals_callback"]

REFERRALS_PLACEHOLDER = (
    "🎁 <b>Реферальная программа</b>\n\n"
    "Раздел скоро появится. Начисления за приглашения пока не производятся — "
    "сообщим, когда программа заработает."
)


async def cmd_referrals(message: Message) -> None:
    await message.answer(
        REFERRALS_PLACEHOLDER,
        reply_markup=main_menu_keyboard(),
    )


@router.callback_query(F.data.startswith("refs:"))
async def legacy_referrals_callback(callback: CallbackQuery) -> None:
    """Turn already-sent referral pagination buttons into a safe placeholder."""

    await safe_edit_text(callback.message, REFERRALS_PLACEHOLDER)
    await safe_callback_answer(callback)
