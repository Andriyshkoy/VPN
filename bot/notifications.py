"""Telegram notification listener for the bot."""

from __future__ import annotations

import asyncio
from aiogram import Bot

from core.services.notifications import NotificationService


async def send_pending_notifications(bot: Bot, service: NotificationService) -> None:
    """Send all queued notifications using the provided bot."""
    pending = await service.get_pending()
    for note in pending:
        try:
            await bot.send_message(note.chat_id, note.text)
        except Exception:
            # Ignore delivery errors
            pass


async def notifications_listener(
    bot: Bot,
    *,
    poll_interval: float = 5.0,
    service: NotificationService | None = None,
) -> None:
    """Continuously poll Redis for notifications and send them."""
    service = service or NotificationService()
    while True:
        await send_pending_notifications(bot, service)
        await asyncio.sleep(poll_interval)
