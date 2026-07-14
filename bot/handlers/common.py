from __future__ import annotations

from aiogram import F
from aiogram.filters import CommandObject
from aiogram.types import CallbackQuery, Message

from core.config import settings
from core.services import TelegramActionAuditContext

from ..instructions import GUIDE_MENU_TEXT, GUIDES
from ..keyboards import (
    balance_actions_keyboard,
    guide_back_keyboard,
    guide_menu_keyboard,
    main_menu_keyboard,
)
from ..ui import format_money, safe_callback_answer, safe_edit_text
from .base import get_or_create_user, router

__all__ = ["cmd_start", "cmd_help", "cmd_how_to_use", "cmd_balance"]


async def cmd_start(
    message: Message,
    command: CommandObject | None = None,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    ref_id = command.args if command and command.args else None
    user = await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        ref_id=ref_id,
    )
    if user is None:
        if telegram_action_audit is not None:
            telegram_action_audit.record(
                "navigation.start",
                result="rejected" if ref_id else "unavailable",
                metadata={
                    "reason_code": "invalid_invite" if ref_id else "account_unavailable"
                },
            )
        return
    await message.answer(
        "👋 <b>Добро пожаловать!</b>\n\n"
        "<b>Простой и доступный VPN без сложных настроек.</b>\n\n"
        "⚡ Подключение за несколько минут\n"
        "🛡 Стабильное соединение для телефона и компьютера\n"
        "💳 Около 50 ₽ в месяц за одну конфигурацию — деньги списываются "
        "постепенно, небольшими частями.\n\n"
        "Пополните баланс, создайте конфигурацию по инструкции — и можно "
        "пользоваться.\n\n"
        "Выберите нужный раздел на клавиатуре 👇",
        reply_markup=main_menu_keyboard(),
    )
    if telegram_action_audit is not None:
        telegram_action_audit.record("navigation.start")


async def cmd_help(
    message: Message,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    await message.answer(
        "❓ <b>Помощь</b>\n\n"
        "Все основные действия доступны на клавиатуре внизу экрана.\n\n"
        "В разделе <b>«Мои конфиги»</b> можно создать новый профиль, скачать "
        "его, переименовать или удалить.\n\n"
        "Если клавиатура пропала, отправьте /menu. Если не получается "
        "подключиться — откройте <b>«Инструкции» → «Не подключается»</b>.\n\n"
        'Нужна помощь человека? Напишите <a href="https://t.me/andriyshkoy">'
        "@andriyshkoy</a>.",
        reply_markup=main_menu_keyboard(),
        disable_web_page_preview=True,
    )
    if telegram_action_audit is not None:
        telegram_action_audit.record("navigation.help")


async def cmd_how_to_use(
    message: Message,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    await message.answer(
        GUIDE_MENU_TEXT,
        reply_markup=guide_menu_keyboard(),
        disable_web_page_preview=True,
    )
    if telegram_action_audit is not None:
        telegram_action_audit.record("navigation.instructions_open")


@router.callback_query(F.data.startswith("guide:"))
async def show_guide(
    callback: CallbackQuery,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    guide = callback.data.partition(":")[2]
    if guide == "menu":
        text = GUIDE_MENU_TEXT
        markup = guide_menu_keyboard()
    elif guide in GUIDES:
        text = GUIDES[guide]
        markup = guide_back_keyboard()
    else:
        await safe_callback_answer(
            callback,
            "Инструкция не найдена. Откройте раздел заново.",
            show_alert=True,
        )
        if telegram_action_audit is not None:
            telegram_action_audit.record(
                "navigation.guide_open",
                result="invalid",
                metadata={"reason_code": "unknown_guide"},
            )
        return

    await safe_edit_text(
        callback.message,
        text,
        reply_markup=markup,
        disable_web_page_preview=True,
    )
    await safe_callback_answer(callback)
    if telegram_action_audit is not None:
        telegram_action_audit.record(
            "navigation.guide_open",
            metadata={"guide": guide},
        )


def render_balance_summary(user) -> str:
    text = "💰 <b>Ваш баланс</b>\n\n" f"Доступно: <b>{format_money(user.balance)} ₽</b>"
    if settings.maintenance_mode or not settings.billing_enabled:
        text += "\n\n⏸ Списания сейчас приостановлены."
    return text


async def cmd_balance(
    message: Message,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    user = await get_or_create_user(
        message.from_user.id,
        message.from_user.username,
    )
    if user is None:
        if telegram_action_audit is not None:
            telegram_action_audit.record(
                "finance.balance_view",
                result="unavailable",
                metadata={"reason_code": "account_unavailable"},
            )
        return
    await message.answer(
        render_balance_summary(user),
        reply_markup=balance_actions_keyboard(),
    )
    if telegram_action_audit is not None:
        telegram_action_audit.record("finance.balance_view")
