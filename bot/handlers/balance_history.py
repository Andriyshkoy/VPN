from __future__ import annotations

from datetime import timezone

from aiogram import F
from aiogram.types import CallbackQuery, Message

from core.db.unit_of_work import uow
from core.services import AccountingService, BalanceHistoryPage

from ..keyboards import balance_actions_keyboard, balance_history_keyboard
from ..ui import format_money, safe_callback_answer, safe_edit_text
from .base import get_or_create_user, router

HISTORY_PAGE_SIZE = 8

_KIND_LABELS = {
    "opening_balance": "Начальный баланс",
    "manual_top_up": "Ручное пополнение",
    "manual_withdrawal": "Ручное списание",
    "admin_adjustment": "Корректировка баланса",
    "provider_payment": "Пополнение через Telegram",
    "periodic_charge": "Оплата VPN",
    "config_reservation": "Создание VPN-конфига",
    "config_refund": "Возврат за VPN-конфиг",
    "referral_reward_l1": "Реферальный бонус · 1 уровень",
    "referral_reward_l2": "Реферальный бонус · 2 уровень",
}

accounting_service = AccountingService(uow)


def render_balance_history(page: BalanceHistoryPage) -> str:
    if not page.items:
        return (
            "📒 <b>История баланса</b>\n\n"
            "Операций пока нет. После первого пополнения или списания они "
            "появятся здесь."
        )

    lines = ["📒 <b>История баланса</b>", ""]
    for item in page.items:
        created_at = item.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        else:
            created_at = created_at.astimezone(timezone.utc)
        sign = "+" if item.amount > 0 else "−"
        label = _KIND_LABELS.get(item.kind, "Операция с балансом")
        lines.extend(
            (
                f"<b>{sign}{format_money(abs(item.amount))} ₽</b> · {label}",
                f"{created_at:%d.%m.%Y %H:%M} UTC · после операции "
                f"{format_money(item.balance_after)} ₽",
                "",
            )
        )

    first = page.offset + 1
    last = page.offset + len(page.items)
    lines.append(f"Операции {first}–{last} из {page.total}")
    return "\n".join(lines)


async def _history_for_telegram_user(
    tg_id: int,
    username: str | None,
    *,
    offset: int,
    snapshot_id: int | None = None,
) -> BalanceHistoryPage | None:
    user = await get_or_create_user(tg_id, username)
    if user is None:
        return None
    return await accounting_service.list_balance_history(
        user.id,
        limit=HISTORY_PAGE_SIZE,
        offset=offset,
        snapshot_id=snapshot_id,
    )


async def cmd_balance_history(message: Message) -> None:
    page = await _history_for_telegram_user(
        message.from_user.id,
        message.from_user.username,
        offset=0,
    )
    if page is None:
        return
    await message.answer(
        render_balance_history(page),
        reply_markup=balance_history_keyboard(
            offset=page.offset,
            limit=page.limit,
            total=page.total,
            snapshot_id=page.snapshot_id,
        ),
    )


@router.callback_query(F.data.startswith("balance_history:"))
async def balance_history_callback(callback: CallbackQuery) -> None:
    raw_cursor = callback.data.partition(":")[2]
    try:
        cursor_parts = raw_cursor.split(":")
        if len(cursor_parts) == 1:
            snapshot_id = None
            offset = int(cursor_parts[0])
        elif len(cursor_parts) == 2:
            snapshot_id = int(cursor_parts[0])
            offset = int(cursor_parts[1])
        else:
            raise ValueError
        if not 0 <= offset <= 1_000_000:
            raise ValueError
        if (
            snapshot_id is not None
            and not 0 <= snapshot_id <= 9_223_372_036_854_775_807
        ):
            raise ValueError
    except ValueError:
        await safe_callback_answer(callback, "Не удалось открыть эту страницу.")
        return

    page = await _history_for_telegram_user(
        callback.from_user.id,
        callback.from_user.username,
        offset=offset,
        snapshot_id=snapshot_id,
    )
    if page is None:
        await safe_callback_answer(callback)
        return
    await safe_edit_text(
        callback.message,
        render_balance_history(page),
        reply_markup=balance_history_keyboard(
            offset=page.offset,
            limit=page.limit,
            total=page.total,
            snapshot_id=page.snapshot_id,
        ),
    )
    await safe_callback_answer(callback)


@router.callback_query(F.data == "balance_summary")
async def balance_summary_callback(callback: CallbackQuery) -> None:
    from .common import render_balance_summary

    user = await get_or_create_user(
        callback.from_user.id,
        callback.from_user.username,
    )
    if user is None:
        await safe_callback_answer(callback)
        return
    await safe_edit_text(
        callback.message,
        render_balance_summary(user),
        reply_markup=balance_actions_keyboard(),
    )
    await safe_callback_answer(callback)
