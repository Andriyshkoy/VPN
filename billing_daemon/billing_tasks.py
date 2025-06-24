import asyncio

from core.config import settings
from core.db.unit_of_work import uow
from core.services import BillingService
from core.services.notifications import NotificationService


async def _charge_all_and_notify_async() -> None:
    billing = BillingService(uow, per_config_cost=settings.per_config_cost)
    charges = await billing.charge_all()

    notifications = NotificationService()
    for user, charge in charges.items():
        balance = user.balance
        if balance <= 0:
            text = (
                "\u26a0\ufe0f Ваши сервисы приостановлены, "
                "пополните счёт."
            )
        else:
            week_high = charge * 24 * 7
            week_low = charge * (24 * 7 - 1)
            day_high = charge * 24
            day_low = charge * 23

            if week_low < balance <= week_high:
                text = (
                    "\u26a0\ufe0f Текущего баланса хватит на неделю, "
                    "советую пополнить счёт."
                )
            elif day_low < balance <= day_high:
                text = (
                    "\u26a0\ufe0f Текущего баланса хватит на сутки, "
                    "советую срочно пополнить счёт."
                )
            else:
                continue
        await notifications.enqueue(user.tg_id, text)


def charge_all_and_notify() -> None:
    """Synchronously run billing and notifications for RQ."""
    asyncio.run(_charge_all_and_notify_async())
