from aiogram import F
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .base import REFERRALS_PER_PAGE, billing_service, require_user, router, user_service

__all__ = ["cmd_referrals", "paginate_referrals"]


async def _send_referrals(
    target: Message | CallbackQuery, user_id: int, tg_id: int, page: int = 0
) -> None:
    total = await user_service.count_referrals(user_id)
    offset = page * REFERRALS_PER_PAGE
    referrals = await user_service.get_referrals(user_id, limit=REFERRALS_PER_PAGE, offset=offset)
    settings = await billing_service.get_settings()

    text = (
        "📊 <b>Ваши рефералы</b>\n\n"
        "Приглашайте друзей и получайте бонусы!\n"
        f"• {settings.referral_first_deposit_bonus_pct}% от первого пополнения\n"
        f"• {settings.referral_recurring_bonus_pct}% от следующих пополнений\n\n"
        f"Ваша реферальная ссылка (Нажмите чтобы скопировать):\n\n<code>https://t.me/andriyshkoy_vpn_bot?start={tg_id}</code>\n\n"
    )

    if not referrals:
        text += "У вас нет рефералов."
        markup = None
    else:
        referral_ids = [ref.id for ref in referrals]
        totals = await billing_service.get_referral_bonus_totals(
            user_id=user_id,
            related_user_ids=referral_ids,
        )
        text += f"Всего: {total}\n\n"
        for ref in referrals:
            name = f"@{ref.username}" if ref.username else f"ID: {ref.tg_id}"
            bonus_total = totals.get(ref.id, 0)
            text += f"• {name} — бонусы: {bonus_total:.2f} ₽\n"

        buttons = []
        if page > 0:
            buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"refs:{page-1}"))
        if offset + REFERRALS_PER_PAGE < total:
            buttons.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"refs:{page+1}"))
        markup = InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None

    send_method = target.answer if isinstance(target, Message) else target.message.edit_text
    await send_method(text, reply_markup=markup, parse_mode="HTML")
    if isinstance(target, CallbackQuery):
        await target.answer()


@router.message(Command("referrals"))
async def cmd_referrals(message: Message) -> None:
    user = await require_user(message)
    if not user:
        return
    await _send_referrals(message, user.id, message.from_user.id, page=0)


@router.message(F.text == "👥 Рефералы")
async def referrals_button(message: Message) -> None:
    user = await require_user(message)
    if not user:
        return
    await _send_referrals(message, user.id, message.from_user.id, page=0)


@router.callback_query(F.data.startswith("refs:"))
async def paginate_referrals(callback: CallbackQuery) -> None:
    try:
        page = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Некорректные данные", show_alert=True)
        return
    user = await require_user(callback)
    if not user:
        return
    await _send_referrals(callback, user.id, callback.from_user.id, page=page)
