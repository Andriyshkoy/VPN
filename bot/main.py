from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from core.config import settings

from .handlers import router


def setup_bot() -> Dispatcher:
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp.include_router(router)
    return dp


async def main():
    bot = Bot(token=settings.bot_token, parse_mode=ParseMode.HTML)
    dp = setup_bot()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
