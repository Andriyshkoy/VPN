import asyncio
import contextlib

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from .notifications import notifications_listener

from core.config import settings

from .handlers import router, setup_bot_commands


def setup_bot() -> Dispatcher:
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp.include_router(router)
    return dp


async def main():
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = setup_bot()

    await setup_bot_commands(bot)
    await bot.delete_webhook(drop_pending_updates=True)
    listener = asyncio.create_task(notifications_listener(bot))
    try:
        await dp.start_polling(bot)
    finally:
        listener.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener


if __name__ == "__main__":
    asyncio.run(main())
