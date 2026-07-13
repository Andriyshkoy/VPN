from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from aiogram.exceptions import TelegramBadRequest


def format_money(value: Decimal | int | str) -> str:
    amount = Decimal(str(value))
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",")


def format_whole_money(value: Decimal | int | str) -> str:
    amount = Decimal(str(value))
    return f"{amount:,.0f}".replace(",", " ")


def estimate_monthly_cost(
    per_period_cost: Decimal | int | str,
    billing_interval_seconds: int,
) -> Decimal:
    """Return a user-facing whole-ruble estimate for a 30-day month."""

    periods_per_month = Decimal(30 * 24 * 60 * 60) / Decimal(billing_interval_seconds)
    return (Decimal(str(per_period_cost)) * periods_per_month).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP,
    )


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
