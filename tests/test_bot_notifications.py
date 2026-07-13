from datetime import datetime, timezone

import fakeredis.aioredis
import pytest
from sqlalchemy import select

from bot.notifications import (
    DeliveryStatus,
    _persist_delivery_status,
    send_pending_notifications,
)
from core.config import settings
from core.db.models.notification_outbox import NotificationOutbox
from core.db.unit_of_work import uow
from core.services import UserService
from core.services.notifications import NotificationService


class DummyBot:
    def __init__(self, error=None):
        self.sent = []
        self.error = error

    async def send_message(self, chat_id, text):
        if self.error is not None:
            raise self.error
        self.sent.append((chat_id, text))


@pytest.mark.asyncio
async def test_send_pending_notifications():
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    service = NotificationService(redis_client)

    await service.enqueue(1, "hello")
    await service.enqueue(2, "bye")

    bot = DummyBot()
    results = await send_pending_notifications(bot, service)

    assert bot.sent == [(1, "hello"), (2, "bye")]
    assert [result.status for result in results] == [
        DeliveryStatus.DELIVERED,
        DeliveryStatus.DELIVERED,
    ]
    assert await redis_client.llen("notifications") == 0
    assert await redis_client.llen("notifications:processing") == 0


@pytest.mark.asyncio
async def test_transient_failure_is_retried_with_backoff(monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr("core.services.notifications.time.time", lambda: now[0])
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    service = NotificationService(redis_client)
    await service.enqueue(1, "hello")

    results = await send_pending_notifications(
        DummyBot(ConnectionError("telegram unavailable")),
        service,
    )

    assert results[0].status is DeliveryStatus.RETRY_SCHEDULED
    assert await redis_client.llen("notifications") == 1
    assert await redis_client.llen("notifications:processing") == 0

    successful_bot = DummyBot()
    await send_pending_notifications(successful_bot, service)
    assert successful_bot.sent == []

    now[0] += 3
    await send_pending_notifications(successful_bot, service)
    assert successful_bot.sent == [(1, "hello")]
    assert await redis_client.llen("notifications") == 0


@pytest.mark.asyncio
async def test_retry_limit_moves_notification_to_failed(monkeypatch):
    monkeypatch.setattr(settings, "notification_max_attempts", 1)
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    service = NotificationService(redis_client)
    await service.enqueue(1, "hello")

    results = await send_pending_notifications(
        DummyBot(ConnectionError("telegram unavailable")), service
    )

    assert results[0].status is DeliveryStatus.RETRIES_EXHAUSTED
    assert await redis_client.llen("notifications") == 0
    assert await redis_client.llen("notifications:failed") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (
            RuntimeError("Forbidden: bot was blocked by the user"),
            DeliveryStatus.BLOCKED,
        ),
        (RuntimeError("Bad Request: user is deactivated"), DeliveryStatus.DEACTIVATED),
    ],
)
async def test_unavailable_recipient_is_classified_and_not_retried(
    error,
    expected_status,
):
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    service = NotificationService(redis_client)
    await service.enqueue(1, "hello")

    results = await send_pending_notifications(DummyBot(error), service)

    assert results[0].status is expected_status
    assert results[0].notification_id
    assert results[0].attempt == 1
    assert await redis_client.llen("notifications") == 0
    assert await redis_client.llen("notifications:processing") == 0
    assert await redis_client.llen("notifications:failed") == 1


@pytest.mark.asyncio
async def test_terminal_delivery_settles_outbox_and_old_result_cannot_win(
    sessionmaker,
):
    user = await UserService(uow).register(808080)
    observed = datetime.now(timezone.utc).timestamp() + 1
    async with uow() as repos:
        await repos["billing"].add_notification_outbox(
            dedupe_key="delivery-status:1",
            chat_id=user.tg_id,
            text="status",
        )

    await _persist_delivery_status(
        "delivery-status:1",
        user.tg_id,
        observed,
        DeliveryStatus.BLOCKED,
        "blocked",
    )
    async with uow() as repos:
        blocked = await repos["users"].get(id=user.id)
        outbox = await repos["users"].session.scalar(select(NotificationOutbox))
    assert blocked.telegram_delivery_status == "blocked"
    assert outbox.status == "failed"

    async with uow() as repos:
        await repos["users"].set_telegram_delivery_status(
            user.tg_id,
            delivery_status="active",
            observed_at=datetime.fromtimestamp(observed + 2, tz=timezone.utc),
        )
        stale = await repos["users"].set_telegram_delivery_status(
            user.tg_id,
            delivery_status="blocked",
            error="old result",
            observed_at=datetime.fromtimestamp(observed + 1, tz=timezone.utc),
        )
    assert stale is None
    async with uow() as repos:
        current = await repos["users"].get(id=user.id)
    assert current.telegram_delivery_status == "active"
