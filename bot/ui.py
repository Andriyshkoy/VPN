from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from aiogram.exceptions import TelegramBadRequest


def format_money(value: Decimal | int | str) -> str:
    amount = Decimal(str(value))
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",")


def format_billing_interval(seconds: int) -> str:
    if seconds == 60:
        return "раз в минуту"
    if seconds % 86_400 == 0:
        days = seconds // 86_400
        return "раз в день" if days == 1 else f"раз в {days} дн."
    if seconds % 3_600 == 0:
        hours = seconds // 3_600
        return "раз в час" if hours == 1 else f"раз в {hours} ч."
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"раз в {minutes} мин."
    return f"раз в {seconds} сек."


def safe_document_filename(value: str | None) -> str:
    normalized = re.sub(r"[^\w .()\-]+", "_", value or "", flags=re.UNICODE)
    normalized = normalized.strip(" .")[:80]
    return f"{normalized or 'vpn-config'}.ovpn"


async def safe_callback_answer(callback: Any, *args: Any, **kwargs: Any) -> None:
    """Treat an already-expired callback acknowledgement as delivered."""

    try:
        await callback.answer(*args, **kwargs)
    except TelegramBadRequest as exc:
        error = str(exc).lower()
        if "query is too old" in error or "query id is invalid" in error:
            return
        raise


async def safe_edit_text(message: Any, text: str, **kwargs: Any) -> bool:
    """Make Telegram message edits replay-safe for the durable update inbox."""

    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return False
        raise
    return True
