"""Telegram notification listener for the bot."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)

from core.config import settings
from core.db.unit_of_work import uow
from core.services.campaigns import PREFIX as CAMPAIGN_PREFIX
from core.services.campaigns import CampaignDeliveryService
from core.services.notifications import Notification, NotificationService

logger = logging.getLogger(__name__)


class DeliveryStatus(str, Enum):
    """Machine-readable outcome for future recipient-state persistence."""

    DELIVERED = "delivered"
    BLOCKED = "blocked"
    DEACTIVATED = "deactivated"
    PERMANENT_FAILURE = "permanent_failure"
    CONTENT_FAILURE = "content_failure"
    RETRY_SCHEDULED = "retry_scheduled"
    RETRIES_EXHAUSTED = "retries_exhausted"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class NotificationDeliveryResult:
    """Result of one delivery attempt."""

    notification_id: str
    notification: Notification
    status: DeliveryStatus
    attempt: int
    error: str | None = None


async def send_pending_notifications(
    bot: Bot,
    service: NotificationService,
    *,
    delivery_recorder: (
        Callable[[str, int, float, DeliveryStatus, str | None], Awaitable[None]] | None
    ) = None,
    max_batch: int = 100,
    lease_guard: Callable[[], Awaitable[bool]] | None = None,
    campaign_delivery: CampaignDeliveryService | None = None,
) -> list[NotificationDeliveryResult]:
    """Deliver one snapshot of the queue with acknowledge/retry semantics."""
    results: list[NotificationDeliveryResult] = []

    # Process only the items visible at the beginning of this pass.  A transient
    # failure is requeued for the next poll instead of being retried in a hot loop.
    count = min(await service.pending_count(), max_batch)
    for _ in range(count):
        if lease_guard is not None and not await lease_guard():
            break
        reserved = await service.reserve()
        if reserved is None:
            break

        if reserved.notification_id.startswith(CAMPAIGN_PREFIX):
            result = await _deliver_campaign_notification(
                bot,
                service,
                reserved,
                campaign_delivery or CampaignDeliveryService(),
                delivery_recorder,
            )
            results.append(result)
            continue

        try:
            await bot.send_message(reserved.chat_id, reserved.text)
        except asyncio.CancelledError:
            # Cancellation commonly happens while the bot is shutting down.  Put
            # the reservation back before propagating it to the caller.
            await asyncio.shield(service.retry(reserved))
            raise
        except Exception as exc:
            status = _classify_delivery_error(exc)
            error = str(exc)

            if (
                status is DeliveryStatus.RETRY_SCHEDULED
                and reserved.attempts + 1 >= settings.notification_max_attempts
            ):
                status = DeliveryStatus.RETRIES_EXHAUSTED

            if status is DeliveryStatus.RETRY_SCHEDULED:
                delay = _retry_delay(exc, reserved.attempts + 1)
                await service.retry(reserved, delay_seconds=delay)
                logger.warning(
                    "Notification delivery will be retried",
                    extra={
                        "chat_id": reserved.chat_id,
                        "notification_id": reserved.notification_id,
                        "attempt": reserved.attempts + 1,
                        "retry_delay": delay,
                    },
                    exc_info=exc,
                )
            else:
                await service.fail(
                    reserved,
                    reason=error,
                    category=status.value,
                )
                logger.info(
                    "Notification recipient is not deliverable",
                    extra={
                        "chat_id": reserved.chat_id,
                        "notification_id": reserved.notification_id,
                        "delivery_status": status.value,
                    },
                )

            if delivery_recorder is not None:
                await _record_delivery_safely(
                    delivery_recorder,
                    reserved.notification_id,
                    reserved.chat_id,
                    reserved.created_at,
                    status,
                    error,
                )

            results.append(
                NotificationDeliveryResult(
                    notification_id=reserved.notification_id,
                    notification=reserved.notification,
                    status=status,
                    attempt=reserved.attempts + 1,
                    error=error,
                )
            )
        else:
            await service.acknowledge(reserved)
            if delivery_recorder is not None:
                await _record_delivery_safely(
                    delivery_recorder,
                    reserved.notification_id,
                    reserved.chat_id,
                    reserved.created_at,
                    DeliveryStatus.DELIVERED,
                    None,
                )
            results.append(
                NotificationDeliveryResult(
                    notification_id=reserved.notification_id,
                    notification=reserved.notification,
                    status=DeliveryStatus.DELIVERED,
                    attempt=reserved.attempts + 1,
                )
            )

    return results


async def _deliver_campaign_notification(bot, service, reserved, delivery, recorder):
    """Campaign-only semantics: never blindly resend an ambiguous Telegram send."""
    # One elected listener and this pacing cap campaign throughput at 20/s.
    await asyncio.sleep(0.05)
    claim = await delivery.claim(
        reserved.notification_id, chat_id=reserved.chat_id, text=reserved.text
    )
    if not claim.allowed:
        if claim.status == "deferred":
            await service.retry(reserved, delay_seconds=max(1, claim.delay_seconds))
            return NotificationDeliveryResult(
                reserved.notification_id,
                reserved.notification,
                DeliveryStatus.RETRY_SCHEDULED,
                reserved.attempts,
            )
        await service.acknowledge(reserved)
        status = {
            "unknown": DeliveryStatus.UNKNOWN,
            "cancelled": DeliveryStatus.CANCELLED,
            "delivered": DeliveryStatus.DELIVERED,
        }.get(claim.status, DeliveryStatus.SKIPPED)
        return NotificationDeliveryResult(
            reserved.notification_id, reserved.notification, status, reserved.attempts
        )

    error = None
    try:
        # Campaigns are plain text even if the bot defaults to HTML parsing.
        await bot.send_message(claim.chat_id, claim.text, parse_mode=None)
    except asyncio.CancelledError:
        await asyncio.shield(
            delivery.finish(
                reserved.notification_id,
                status="unknown",
                error="cancelled_during_send",
            )
        )
        await asyncio.shield(
            service.fail(reserved, reason="cancelled_during_send", category="unknown")
        )
        raise
    except Exception as exc:
        error = type(exc).__name__
        if isinstance(exc, TelegramRetryAfter):
            if reserved.attempts + 1 >= settings.notification_max_attempts:
                status = DeliveryStatus.RETRIES_EXHAUSTED
                await delivery.finish(
                    reserved.notification_id,
                    status="failed",
                    error="rate_limit_exhausted",
                )
                await service.fail(
                    reserved, reason="rate_limit_exhausted", category=status.value
                )
            else:
                status = DeliveryStatus.RETRY_SCHEDULED
                delay = _retry_delay(exc, reserved.attempts + 1)
                await delivery.finish(
                    reserved.notification_id,
                    status="retry",
                    error="rate_limited",
                    retry_after=delay,
                )
                await service.retry(reserved, delay_seconds=delay)
        elif isinstance(
            exc, (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound)
        ):
            status = _classify_delivery_error(exc)
            await delivery.finish(
                reserved.notification_id, status="failed", error=status.value
            )
            await service.fail(reserved, reason=status.value, category=status.value)
        else:
            # A timeout or disconnected response does not prove that Telegram
            # rejected the message. Manual status review must come before resend.
            status = DeliveryStatus.UNKNOWN
            await delivery.finish(
                reserved.notification_id, status="unknown", error=error
            )
            await service.fail(reserved, reason=error, category="unknown")
    else:
        status = DeliveryStatus.DELIVERED
        # If outcome persistence fails, leave the Redis reservation recoverable;
        # its existing durable 'sending' claim prevents a duplicate Telegram send.
        await delivery.finish(reserved.notification_id, status="delivered")
        await service.acknowledge(reserved)
    if recorder is not None:
        await _record_delivery_safely(
            recorder,
            reserved.notification_id,
            reserved.chat_id,
            reserved.created_at,
            status,
            error,
        )
    return NotificationDeliveryResult(
        reserved.notification_id,
        reserved.notification,
        status,
        reserved.attempts + 1,
        error,
    )


async def notifications_listener(
    bot: Bot,
    *,
    poll_interval: float = 5.0,
    service: NotificationService | None = None,
) -> None:
    """Continuously poll Redis for notifications and send them."""
    service = service or NotificationService()
    while True:
        if not settings.notifications_enabled:
            await asyncio.sleep(poll_interval)
            continue
        try:
            async with service.listener_lock() as acquired:
                if acquired:
                    # Only the elected consumer may recover in-flight rows. This
                    # keeps a rolling second replica from stealing active work.
                    await CampaignDeliveryService().reconcile_stale()
                    await service.recover_processing()
                    await send_pending_notifications(
                        bot,
                        service,
                        delivery_recorder=_persist_delivery_status,
                        lease_guard=service.listener_lease_is_valid,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            # A temporary Redis outage must not permanently kill the background
            # task.  The next pass will recover any unfinished reservation.
            logger.exception("Notification listener pass failed")
        await asyncio.sleep(poll_interval)


def _classify_delivery_error(exc: Exception) -> DeliveryStatus:
    """Separate permanent recipient failures from retryable transport errors."""
    message = str(exc).casefold()
    if "deactivated" in message:
        return DeliveryStatus.DEACTIVATED
    if "blocked" in message or "bot was kicked" in message:
        return DeliveryStatus.BLOCKED
    if isinstance(exc, TelegramForbiddenError):
        return DeliveryStatus.BLOCKED
    if isinstance(exc, TelegramNotFound):
        return DeliveryStatus.PERMANENT_FAILURE
    if isinstance(exc, TelegramBadRequest):
        return DeliveryStatus.CONTENT_FAILURE
    return DeliveryStatus.RETRY_SCHEDULED


def _retry_delay(exc: Exception, attempt: int) -> float:
    if isinstance(exc, TelegramRetryAfter):
        return max(1.0, float(exc.retry_after))
    return float(min(300, 2 ** min(max(attempt, 1), 8)))


async def _persist_delivery_status(
    notification_id: str,
    chat_id: int,
    created_at: float,
    status: DeliveryStatus,
    error: str | None,
) -> None:
    observed_at = datetime.fromtimestamp(created_at, tz=timezone.utc)
    async with uow() as repos:
        if status in {
            DeliveryStatus.DELIVERED,
            DeliveryStatus.BLOCKED,
            DeliveryStatus.DEACTIVATED,
            DeliveryStatus.PERMANENT_FAILURE,
        }:
            stored_status = (
                "active" if status is DeliveryStatus.DELIVERED else status.value
            )
            await repos["users"].set_telegram_delivery_status(
                chat_id,
                delivery_status=stored_status,
                error=error,
                observed_at=observed_at,
            )
        if notification_id.startswith(CAMPAIGN_PREFIX):
            # The campaign guard persists its stricter outcome independently.
            return
        if status is DeliveryStatus.RETRY_SCHEDULED:
            await repos["billing"].touch_notification_outbox(
                dedupe_key=notification_id,
            )
        else:
            await repos["billing"].settle_notification_outbox(
                dedupe_key=notification_id,
                delivered=status is DeliveryStatus.DELIVERED,
                error=error,
            )


async def _record_delivery_safely(
    recorder: Callable[[str, int, float, DeliveryStatus, str | None], Awaitable[None]],
    notification_id: str,
    chat_id: int,
    created_at: float,
    status: DeliveryStatus,
    error: str | None,
) -> None:
    try:
        await recorder(notification_id, chat_id, created_at, status, error)
    except Exception:
        # Telegram send/queue handling already completed. A temporary database
        # problem must not turn it into a duplicate delivery loop.
        logger.exception(
            "Failed to persist Telegram delivery status",
            extra={"chat_id": chat_id, "delivery_status": status.value},
        )
