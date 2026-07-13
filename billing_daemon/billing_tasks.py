import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from core.config import settings
from core.db.unit_of_work import uow
from core.observability import observe_background_job, observe_outbox_publish
from core.services import BillingService
from core.services.notifications import NotificationService

logger = logging.getLogger(__name__)

REFERRAL_RECONCILE_BATCH_SIZE = 100


async def _charge_all_and_notify_async() -> bool:
    if settings.maintenance_mode or not settings.billing_enabled:
        return False

    billing = BillingService(uow, per_config_cost=settings.per_config_cost)
    await billing.charge_all()
    await _publish_notification_outbox_async()
    return True


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
                observe_outbox_publish("error")
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
                observe_outbox_publish("published")
                published += 1
    return published


async def _reconcile_vpn_operations_async() -> bool:
    if settings.maintenance_mode:
        return False
    billing = BillingService(uow, per_config_cost=settings.per_config_cost)
    await billing.reconcile_pending_config_operations()
    return True


async def _reconcile_referral_rewards_async(
    *, limit: int = REFERRAL_RECONCILE_BATCH_SIZE
) -> bool:
    """Catch up durable provider payments that have no referral settlement yet."""

    if settings.maintenance_mode or not settings.referral_rewards_enabled:
        return False

    billing = BillingService(uow, per_config_cost=settings.per_config_cost)
    await billing.reconcile_referral_rewards(limit=limit)
    return True


def _run_observed_job(
    name: str,
    function: Callable[[], Awaitable[object]],
) -> object:
    started = time.monotonic()
    outcome = "error"
    try:
        result = asyncio.run(function())
        outcome = "skipped" if result is False else "success"
        return result
    finally:
        observe_background_job(name, outcome, time.monotonic() - started)


def charge_all_and_notify() -> None:
    """Synchronously run billing and notifications for RQ."""
    _run_observed_job("billing", _charge_all_and_notify_async)


def reconcile_vpn_operations() -> None:
    """Synchronously reconcile durable VPN operations for RQ."""

    _run_observed_job("vpn_reconcile", _reconcile_vpn_operations_async)


def reconcile_referral_rewards() -> None:
    """Synchronously reconcile referral rewards for RQ."""

    _run_observed_job("referral_reconcile", _reconcile_referral_rewards_async)


def publish_notification_outbox() -> None:
    """Synchronously publish pending outbox rows for RQ."""

    _run_observed_job("notification_outbox", _publish_notification_outbox_async)
