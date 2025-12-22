import asyncio
from decimal import Decimal

from core.db.unit_of_work import uow
from core.services import BillingService, ConfigService
from core.services.notifications import NotificationService

HOURS_PER_DAY = Decimal("24")
HOURS_PER_WEEK = Decimal("168")


async def _charge_all_and_notify_async() -> None:
    billing = BillingService(uow)
    charges = await billing.charge_usage()
    settings = await billing.get_settings()
    hourly_rate = settings.monthly_config_cost / Decimal("720")

    config_service = ConfigService(uow)
    notifications = NotificationService()
    for user in charges.keys():
        balance = user.balance
        if balance <= 0:
            text = (
                "🔌 Похоже, баланс закончился, и VPN поставлен на паузу.\n"
                "Как только пополните счёт — всё снова заработает. 😉"
            )
            await notifications.enqueue(user.tg_id, text)
            continue

        active_count = await config_service.count_active(user.id)
        if active_count <= 0:
            continue
        hourly_burn = hourly_rate * Decimal(active_count)
        if hourly_burn <= 0:
            continue

        remaining_hours = balance / hourly_burn
        if remaining_hours <= HOURS_PER_DAY:
            text = (
                f"⚠️ Баланса хватит примерно на сутки.\n"
                f"Пожалуйста, пополните счёт, чтобы не потерять доступ к VPN.\n"
                f"💰 Текущий баланс: {user.balance:.2f} руб."
            )
        elif remaining_hours <= HOURS_PER_WEEK:
            text = (
                f"🔔 Напоминаем: вашего баланса примерно хватит на неделю.\n"
                f"Чтобы избежать перебоев в работе VPN, рекомендуем пополнить счёт заранее.\n"
                f"💰 Текущий баланс: {user.balance:.2f} руб."
            )
        else:
            continue
        await notifications.enqueue(user.tg_id, text)


def charge_all_and_notify() -> None:
    """Synchronously run billing and notifications for RQ."""
    asyncio.run(_charge_all_and_notify_async())
