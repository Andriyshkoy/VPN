import asyncio
from datetime import timedelta

import fakeredis.aioredis
import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessage
from sqlalchemy import func, select

from bot.notifications import (
    DeliveryStatus,
    _persist_delivery_status,
    send_pending_notifications,
)
from core.db.models import CampaignRecipient, NotificationOutbox, User
from core.services.campaigns import CampaignDeliveryService, CampaignService, now_utc
from core.services.notifications import NotificationService


class CampaignBot:
    def __init__(self, error=None):
        self.sent = []
        self.calls = []
        self.error = error

    async def send_message(self, chat_id, text, **kwargs):
        self.calls.append((chat_id, text, kwargs))
        if self.error:
            raise self.error
        self.sent.append((chat_id, text, kwargs))


async def setup_campaign(maker):
    async with maker() as session, session.begin():
        user = User(tg_id=887766)
        session.add(user)
        await session.flush()
        service = CampaignService(session)
        campaign = await service.create(
            name="Test only",
            text="literal <b> & text",
            audience={"kind": "selected", "user_ids": [user.id]},
        )
        preview = await service.preview(campaign["id"])
        await service.launch(
            campaign["id"],
            revision=preview["revision"],
            preview_token=preview["preview_token"],
            expected_recipient_count=1,
            idempotency_key="test-launch-key",
        )
        outbox = await session.scalar(select(NotificationOutbox))
        return campaign, outbox


async def queued(outbox):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = NotificationService(redis)
    await queue.enqueue(outbox.chat_id, outbox.text, notification_id=outbox.dedupe_key)
    return queue


async def status_of(maker):
    async with maker() as session:
        row = await session.scalar(select(CampaignRecipient))
        outbox = await session.get(NotificationOutbox, row.outbox_id)
        return row.status, outbox.status, row.last_error


async def test_campaign_plain_text_success_is_durable_before_ack_and_replay_safe(
    sessionmaker,
):
    _, outbox = await setup_campaign(sessionmaker)
    queue = await queued(outbox)
    bot = CampaignBot()
    results = await send_pending_notifications(bot, queue)
    assert results[0].status == DeliveryStatus.DELIVERED
    assert bot.sent == [(outbox.chat_id, outbox.text, {"parse_mode": None})]
    assert await status_of(sessionmaker) == ("delivered", "delivered", None)
    # Expiry/loss of the Redis dedupe marker must not resend a completed row.
    await queue._redis.delete(f"notifications:enqueued:{outbox.dedupe_key}")
    await queue.enqueue(outbox.chat_id, outbox.text, notification_id=outbox.dedupe_key)
    await send_pending_notifications(bot, queue)
    assert len(bot.calls) == 1


async def test_timeout_is_unknown_not_retried_and_never_stores_message_in_error(
    sessionmaker,
):
    campaign, outbox = await setup_campaign(sessionmaker)
    queue = await queued(outbox)
    bot = CampaignBot(TimeoutError("secret text and personal identifiers"))
    result = (await send_pending_notifications(bot, queue))[0]
    assert result.status == DeliveryStatus.UNKNOWN
    assert await queue.pending_count() == 0
    assert await status_of(sessionmaker) == ("unknown", "unknown", "TimeoutError")
    async with sessionmaker() as session, session.begin():
        summary = await CampaignService(session).retry_failed(
            campaign["id"], idempotency_key="cannot-retry-unknown"
        )
        assert summary["counts"]["unknown"] == 1
        assert await session.scalar(select(func.count(NotificationOutbox.id))) == 1
    await queue._redis.delete(f"notifications:enqueued:{outbox.dedupe_key}")
    await queue.enqueue(outbox.chat_id, outbox.text, notification_id=outbox.dedupe_key)
    await send_pending_notifications(bot, queue)
    assert len(bot.calls) == 1


async def test_crashed_after_claim_recovers_to_unknown_without_bot_call(sessionmaker):
    _, outbox = await setup_campaign(sessionmaker)
    queue = await queued(outbox)
    guard = CampaignDeliveryService(sessionmaker)
    assert (
        await guard.claim(outbox.dedupe_key, chat_id=outbox.chat_id, text=outbox.text)
    ).allowed
    bot = CampaignBot()
    result = (await send_pending_notifications(bot, queue))[0]
    assert result.status == DeliveryStatus.UNKNOWN and bot.calls == []
    assert (await status_of(sessionmaker))[:2] == ("unknown", "unknown")


async def test_cancel_before_send_skips_already_redis_queued_recipient(sessionmaker):
    campaign, outbox = await setup_campaign(sessionmaker)
    queue = await queued(outbox)
    async with sessionmaker() as session, session.begin():
        await CampaignService(session).cancel(
            campaign["id"], idempotency_key="cancel-before-send"
        )
    bot = CampaignBot()
    result = (await send_pending_notifications(bot, queue))[0]
    assert result.status == DeliveryStatus.CANCELLED and bot.calls == []


async def test_429_honors_delay_in_redis_and_pg_outbox(sessionmaker, monkeypatch):
    _, outbox = await setup_campaign(sessionmaker)
    queue = await queued(outbox)
    clock = [1000.0]
    baseline = now_utc()
    monkeypatch.setattr(
        "core.services.campaigns.now_utc",
        lambda: baseline + timedelta(seconds=clock[0] - 1000),
    )
    monkeypatch.setattr("core.services.notifications.time.time", lambda: clock[0])
    method = SendMessage(chat_id=outbox.chat_id, text=outbox.text)
    bot = CampaignBot(
        TelegramRetryAfter(method=method, message="Flood control", retry_after=400)
    )
    result = (await send_pending_notifications(bot, queue))[0]
    assert result.status == DeliveryStatus.RETRY_SCHEDULED
    assert (await status_of(sessionmaker))[:2] == ("pending", "queued")
    async with sessionmaker() as session:
        row = await session.get(NotificationOutbox, outbox.id)
        published = row.published_at.replace(tzinfo=now_utc().tzinfo)
        assert published > now_utc()
    healthy = CampaignBot()
    await send_pending_notifications(healthy, queue)
    assert healthy.calls == []
    clock[0] += 401
    await send_pending_notifications(healthy, queue)
    assert len(healthy.sent) == 1


@pytest.mark.parametrize(
    "error_class, status",
    [
        (TelegramForbiddenError, DeliveryStatus.BLOCKED),
        (TelegramBadRequest, DeliveryStatus.CONTENT_FAILURE),
    ],
)
async def test_explicit_rejection_is_failed_and_updates_delivery_status(
    sessionmaker, error_class, status
):
    _, outbox = await setup_campaign(sessionmaker)
    queue = await queued(outbox)
    method = SendMessage(chat_id=outbox.chat_id, text=outbox.text)
    result = (
        await send_pending_notifications(
            CampaignBot(error_class(method=method, message="explicit rejection")),
            queue,
            delivery_recorder=_persist_delivery_status,
        )
    )[0]
    assert result.status == status
    assert (await status_of(sessionmaker))[:2] == ("failed", "failed")
    if status is DeliveryStatus.BLOCKED:
        async with sessionmaker() as session:
            assert (
                await session.scalar(select(User))
            ).telegram_delivery_status == "blocked"


async def test_shutdown_during_send_is_unknown_and_not_requeued(sessionmaker):
    _, outbox = await setup_campaign(sessionmaker)
    queue = await queued(outbox)
    with pytest.raises(asyncio.CancelledError):
        await send_pending_notifications(CampaignBot(asyncio.CancelledError()), queue)
    assert (await status_of(sessionmaker))[:2] == ("unknown", "unknown")
    assert await queue.pending_count() == 0


async def test_result_db_failure_after_send_cannot_duplicate_on_recovery(sessionmaker):
    _, outbox = await setup_campaign(sessionmaker)
    queue = await queued(outbox)
    guard = CampaignDeliveryService(sessionmaker)

    async def db_unavailable(*args, **kwargs):
        raise ConnectionError("database unavailable")

    guard.finish = db_unavailable
    bot = CampaignBot()
    with pytest.raises(ConnectionError):
        await send_pending_notifications(bot, queue, campaign_delivery=guard)
    assert len(bot.sent) == 1
    await queue.recover_processing()
    await send_pending_notifications(bot, queue)
    assert len(bot.sent) == 1
    assert (await status_of(sessionmaker))[:2] == ("unknown", "unknown")


async def test_retry_delay_survives_crash_before_redis_envelope_update(sessionmaker):
    _, outbox = await setup_campaign(sessionmaker)
    queue = await queued(outbox)
    guard = CampaignDeliveryService(sessionmaker)
    claim = await guard.claim(
        outbox.dedupe_key, chat_id=outbox.chat_id, text=outbox.text
    )
    assert claim.allowed
    # Telegram rejected with retry_after. PostgreSQL outcome committed, then the
    # process died before service.retry updated the original Redis envelope.
    await guard.finish(
        outbox.dedupe_key, status="retry", retry_after=400, error="rate_limited"
    )
    bot = CampaignBot()
    result = (await send_pending_notifications(bot, queue))[0]
    assert result.status == DeliveryStatus.RETRY_SCHEDULED
    assert bot.calls == []
    assert await queue.pending_count() == 1
    assert (await status_of(sessionmaker))[:2] == ("pending", "queued")


async def test_lost_redis_reservation_stale_claim_is_reconciled_without_resend(
    sessionmaker,
):
    campaign, outbox = await setup_campaign(sessionmaker)
    queue = await queued(outbox)
    guard = CampaignDeliveryService(sessionmaker)
    await guard.claim(outbox.dedupe_key, chat_id=outbox.chat_id, text=outbox.text)
    await queue._redis.flushall()
    assert await guard.reconcile_stale() == 0  # Never steal a live request.
    assert await guard.reconcile_stale(before=now_utc() + timedelta(seconds=1)) == 1
    assert (await status_of(sessionmaker))[:2] == ("unknown", "unknown")
    async with sessionmaker() as session:
        assert (await CampaignService(session).get(campaign["id"]))[
            "status"
        ] == "completed"
    # A restored stale Redis copy also remains safe after reconciliation.
    await queue.enqueue(outbox.chat_id, outbox.text, notification_id=outbox.dedupe_key)
    bot = CampaignBot()
    await send_pending_notifications(bot, queue)
    assert bot.calls == []
