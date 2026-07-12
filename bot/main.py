import asyncio
import contextlib
import signal

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage

from core.config import settings
from core.db.unit_of_work import uow
from core.services.telegram_updates import TelegramUpdateService

from .handlers import router, setup_bot_commands
from .notifications import notifications_listener
from .update_ingress import TelegramPollingIngestor, TelegramUpdateProcessor


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
    # Never discard updates received while the bot was down.
    await bot.delete_webhook(drop_pending_updates=False)

    update_service = TelegramUpdateService(uow)
    poller = TelegramPollingIngestor(
        bot,
        update_service,
        allowed_updates=dp.resolve_used_update_types(),
    )
    processors = [
        TelegramUpdateProcessor(
            bot,
            dp,
            update_service,
            cleanup_enabled=index == 0,
        )
        for index in range(settings.telegram_update_processor_count)
    ]
    workflow_data = {
        "dispatcher": dp,
        "bots": (bot,),
        **dp.workflow_data,
    }
    workflow_data.pop("bot", None)
    await dp.emit_startup(bot=bot, **workflow_data)
    tasks = [
        asyncio.create_task(poller.run()),
        *(asyncio.create_task(processor.run()) for processor in processors),
        asyncio.create_task(notifications_listener(bot)),
    ]
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for shutdown_signal in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(shutdown_signal, stop_event.set)
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait(
            [*tasks, stop_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task not in done:
            await asyncio.gather(*done)
    finally:
        for task in [*tasks, stop_task]:
            task.cancel()
        for task in [*tasks, stop_task]:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        try:
            await dp.emit_shutdown(bot=bot, **workflow_data)
        finally:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
