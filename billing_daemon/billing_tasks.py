import asyncio
from functools import lru_cache

from aiohttp import ClientSession
from aiogram import Bot

from core.config import settings
from core.db.unit_of_work import uow
from core.services import BillingService


LOW_BALANCE_THRESHOLD = 10


@lru_cache()
def _get_bot() -> Bot:
    """Return a singleton Bot instance with shared HTTP session."""
    session = ClientSession()
    return Bot(token=settings.bot_token, session=session)


async def _charge_all_and_notify_async() -> None:
    billing = BillingService(uow, per_config_cost=settings.per_config_cost)
    users = await billing.charge_all()

    bot = _get_bot()
    for user in users:
        if user.balance < LOW_BALANCE_THRESHOLD:
            text = (
                f"\u26a0\ufe0f Ваш баланс {user.balance} "
                f"меньше {LOW_BALANCE_THRESHOLD}. Пополните счёт."
            )
            await bot.send_message(user.tg_id, text)


def charge_all_and_notify() -> None:
    """Synchronously run billing and notifications for RQ."""
    asyncio.run(_charge_all_and_notify_async())
