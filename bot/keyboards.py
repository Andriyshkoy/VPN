from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

MENU_BALANCE = "💰 Баланс"
MENU_CONFIGS = "🗂 Мои конфиги"
MENU_TOP_UP = "💳 Пополнить"
MENU_INSTRUCTIONS = "📚 Инструкции"
MENU_REFERRALS = "🎁 Реферальная программа"
MENU_CANCEL = "❌ Отмена"

GUIDE_LABELS = {
    "windows": "🪟 Windows",
    "macos": "🍎 macOS",
    "android": "🤖 Android",
    "ios": "📱 iPhone / iPad",
    "linux": "🐧 Linux",
    "tv": "📺 ТВ / роутер",
    "troubleshooting": "🛠 Не подключается",
}


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Return the persistent, user-facing navigation keyboard."""

    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=MENU_BALANCE),
                KeyboardButton(text=MENU_CONFIGS),
            ],
            [
                KeyboardButton(text=MENU_TOP_UP),
                KeyboardButton(text=MENU_INSTRUCTIONS),
            ],
            [KeyboardButton(text=MENU_REFERRALS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
        input_field_placeholder="Выберите действие",
    )


def balance_actions_keyboard() -> InlineKeyboardMarkup:
    """Actions available from the balance summary."""

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📒 История операций",
                    callback_data="balance_history:0",
                )
            ]
        ]
    )


def balance_history_keyboard(
    *,
    offset: int,
    limit: int,
    total: int,
    snapshot_id: int,
) -> InlineKeyboardMarkup:
    """Bounded pagination for a user's private ledger history."""

    navigation: list[InlineKeyboardButton] = []
    if offset > 0:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️ Новее",
                callback_data=(
                    f"balance_history:{snapshot_id}:{max(0, offset - limit)}"
                ),
            )
        )
    if offset + limit < total:
        navigation.append(
            InlineKeyboardButton(
                text="Старее ➡️",
                callback_data=f"balance_history:{snapshot_id}:{offset + limit}",
            )
        )

    rows = [navigation] if navigation else []
    rows.append(
        [InlineKeyboardButton(text="💰 К балансу", callback_data="balance_summary")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def referral_program_keyboard(share_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📨 Поделиться ссылкой", url=share_url)],
            [
                InlineKeyboardButton(
                    text="📒 История баланса",
                    callback_data="balance_history:0",
                )
            ],
        ]
    )


def cancel_keyboard() -> ReplyKeyboardMarkup:
    """Temporarily replace the main menu while text input is expected."""

    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=MENU_CANCEL)]],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Введите название или отмените действие",
    )


def guide_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=GUIDE_LABELS["windows"], callback_data="guide:windows"
                ),
                InlineKeyboardButton(
                    text=GUIDE_LABELS["macos"], callback_data="guide:macos"
                ),
            ],
            [
                InlineKeyboardButton(
                    text=GUIDE_LABELS["android"], callback_data="guide:android"
                ),
                InlineKeyboardButton(
                    text=GUIDE_LABELS["ios"], callback_data="guide:ios"
                ),
            ],
            [
                InlineKeyboardButton(
                    text=GUIDE_LABELS["linux"], callback_data="guide:linux"
                ),
                InlineKeyboardButton(text=GUIDE_LABELS["tv"], callback_data="guide:tv"),
            ],
            [
                InlineKeyboardButton(
                    text=GUIDE_LABELS["troubleshooting"],
                    callback_data="guide:troubleshooting",
                )
            ],
        ]
    )


def guide_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Все устройства", callback_data="guide:menu")]
        ]
    )
