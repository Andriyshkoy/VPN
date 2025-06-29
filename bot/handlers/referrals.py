from aiogram import F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.filters import Command

from .base import router, user_service, get_or_create_user, REFERRALS_PER_PAGE

__all__ = ["cmd_refferals", "paginate_referrals"]

async def _send_referrals(target: Message | CallbackQuery, user_id: int, tg_id: int, page: int = 0) -> None:
    total = await user_service.count_referrals(user_id)
    offset = page * REFERRALS_PER_PAGE
    referrals = await user_service.get_referrals(user_id, limit=REFERRALS_PER_PAGE, offset=offset)

    text = (
        "📊 <b>Ваши рефералы</b>\n\n"
        "Приглашайте друзей и получайте бонусы!\n"
        f"Ваша реферальная ссылка:\n<code>https://t.me/andriyshkoy_vpn_bot?start={tg_id}</code>\n\n"
    )

    if not referrals:
        text += "У вас нет рефералов."
        markup = None
    else:
        text += f"Всего: {total}\n\n"
        for ref in referrals:
            name = f"@{ref.username}" if ref.username else f"ID: {ref.tg_id}"
            text += f"• {name}\n"

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


@router.message(Command("refferals"))
async def cmd_refferals(message: Message) -> None:
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    await _send_referrals(message, user.id, message.from_user.id, page=0)


@router.callback_query(F.data.startswith("refs:"))
async def paginate_referrals(callback: CallbackQuery) -> None:
    try:
        page = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Некорректные данные", show_alert=True)
        return
    user = await get_or_create_user(callback.from_user.id, callback.from_user.username)
    await _send_referrals(callback, user.id, callback.from_user.id, page=page)
