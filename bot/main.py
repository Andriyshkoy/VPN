import asyncio
import contextlib

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage

from core.config import settings

from .handlers import router, setup_bot_commands
from .notifications import notifications_listener


def setup_bot() -> Dispatcher:
    storage = RedisStorage.from_url(settings.redis_url)
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
    # Never discard successful-payment updates received while the bot was down.
    await bot.delete_webhook(drop_pending_updates=False)
    listener = asyncio.create_task(notifications_listener(bot))
    try:
        await dp.start_polling(bot)
    finally:
        listener.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener


if __name__ == "__main__":
    asyncio.run(main())
