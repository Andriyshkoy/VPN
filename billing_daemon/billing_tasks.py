import asyncio
import logging

from core.config import settings
from core.db.unit_of_work import uow
from core.services import BillingService
from core.services.notifications import NotificationService

logger = logging.getLogger(__name__)


async def _charge_all_and_notify_async() -> None:
    if settings.maintenance_mode or not settings.billing_enabled:
        return

    billing = BillingService(uow, per_config_cost=settings.per_config_cost)
    await billing.charge_all()
    await _publish_notification_outbox_async()


async def _publish_notification_outbox_async(*, limit: int = 20) -> int:
    """Move committed PostgreSQL outbox rows into Redis without loss."""

    if not settings.notifications_enabled:
        return 0

    notifications = NotificationService()
    published = 0
    async with uow() as repos:
        billing_repo = repos["billing"]
        items = await billing_repo.claim_notification_outbox(limit=limit)
        for item in items:
            try:
                await notifications.enqueue(
                    item.chat_id,
                    item.text,
                    notification_id=item.dedupe_key,
                )
            except Exception as exc:
                logger.exception(
                    "Failed to publish billing notification outbox row",
                    extra={"outbox_id": item.id},
                )
                await billing_repo.mark_notification_retry(
                    item,
                    f"{type(exc).__name__}: {exc}",
                )
            else:
                # enqueue() returning False means the stable ID was already
                # published before a prior process crashed; that is success.
                await billing_repo.mark_notification_published(item)
                published += 1
    return published


async def _reconcile_vpn_operations_async() -> None:
    if settings.maintenance_mode:
        return
    billing = BillingService(uow, per_config_cost=settings.per_config_cost)
    await billing.reconcile_pending_config_operations()


def charge_all_and_notify() -> None:
    """Synchronously run billing and notifications for RQ."""
    asyncio.run(_charge_all_and_notify_async())


def reconcile_vpn_operations() -> None:
    """Synchronously reconcile durable VPN operations for RQ."""

    asyncio.run(_reconcile_vpn_operations_async())


def publish_notification_outbox() -> None:
    """Synchronously publish pending outbox rows for RQ."""

    asyncio.run(_publish_notification_outbox_async())
