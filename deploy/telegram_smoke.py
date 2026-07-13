"""Read-only Telegram Bot API smoke test."""

from __future__ import annotations

import asyncio

from aiogram import Bot

from core.config import settings


async def main() -> None:
    bot = Bot(settings.bot_token)
    try:
        identity = await bot.get_me()
        if not identity.username:
            raise RuntimeError("Telegram bot username is missing")
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise RuntimeError("Telegram webhook unexpectedly remains configured")
    finally:
        await bot.session.close()

    print("telegram_smoke=ok")


if __name__ == "__main__":
    asyncio.run(main())
