import asyncio

from aiogram import Bot

from core.config import settings
from core.db.unit_of_work import uow
from core.services import BillingService


LOW_BALANCE_THRESHOLD = 10


async def _charge_all_and_notify_async() -> None:
    billing = BillingService(uow, per_config_cost=settings.per_config_cost)
    charged_users = await billing.charge_all()
    users = charged_users

    bot = Bot(token=settings.bot_token)
    try:
        for user in users:
            if user.balance < LOW_BALANCE_THRESHOLD:
                text = (
                    f"\u26a0\ufe0f Ваш баланс {user.balance} "
                    f"меньше {LOW_BALANCE_THRESHOLD}. Пополните счёт."
                )
                await bot.send_message(user.tg_id, text)
    finally:
        await bot.session.close()


def charge_all_and_notify() -> None:
    """Synchronously run billing and notifications for RQ."""
    asyncio.run(_charge_all_and_notify_async())
