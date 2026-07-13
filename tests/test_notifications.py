import fakeredis.aioredis
import pytest

from core.services.notifications import Notification, NotificationService


@pytest.mark.asyncio
async def test_notification_queue(monkeypatch):
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    service = NotificationService(redis_client)

    await service.enqueue(1, "hello")
    await service.enqueue(2, "bye")

    messages = await service.get_pending()
    assert messages == [
        Notification(chat_id=1, text="hello"),
        Notification(chat_id=2, text="bye"),
    ]
    assert await redis_client.llen("notifications") == 0


@pytest.mark.asyncio
async def test_stable_notification_id_is_enqueued_once():
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    service = NotificationService(redis_client)

    assert await service.enqueue(1, "hello", notification_id="outbox:42") is True
    assert await service.enqueue(1, "hello", notification_id="outbox:42") is False

    assert await redis_client.llen("notifications") == 1
    reserved = await service.reserve()
    assert reserved is not None
    assert reserved.notification_id == "outbox:42"


@pytest.mark.asyncio
async def test_listener_lock_allows_only_one_consumer():
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    first = NotificationService(redis_client)
    second = NotificationService(redis_client)

    async with first.listener_lock() as first_acquired:
        assert first_acquired is True
        async with second.listener_lock() as second_acquired:
            assert second_acquired is False

    async with second.listener_lock() as acquired_after_release:
        assert acquired_after_release is True


@pytest.mark.asyncio
async def test_notification_is_kept_until_acknowledged():
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    service = NotificationService(redis_client)

    await service.enqueue(1, "hello")
    reserved = await service.reserve()

    assert reserved is not None
    assert reserved.notification == Notification(chat_id=1, text="hello")
    assert await redis_client.llen("notifications") == 0
    assert await redis_client.llen("notifications:processing") == 1

    assert await service.acknowledge(reserved) is True
    assert await redis_client.llen("notifications:processing") == 0


@pytest.mark.asyncio
async def test_retry_returns_notification_to_pending_queue():
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    service = NotificationService(redis_client)

    await service.enqueue(1, "hello")
    reserved = await service.reserve()
    assert reserved is not None

    assert await service.retry(reserved) is True
    retried = await service.reserve()

    assert retried is not None
    assert retried.notification == reserved.notification
    assert retried.notification_id == reserved.notification_id
    assert retried.attempts == 1

    # A stale receipt cannot enqueue the same retry twice.
    assert await service.retry(reserved) is False
    assert await redis_client.llen("notifications") == 0


@pytest.mark.asyncio
async def test_delayed_retry_does_not_block_ready_notification(monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr("core.services.notifications.time.time", lambda: now[0])
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    service = NotificationService(redis_client)

    await service.enqueue(1, "delayed")
    delayed = await service.reserve()
    assert delayed is not None
    await service.retry(delayed, delay_seconds=30)
    await service.enqueue(2, "ready")

    ready = await service.reserve()
    assert ready is not None
    assert ready.chat_id == 2
    await service.acknowledge(ready)
    assert await service.reserve() is None

    now[0] += 31
    due = await service.reserve()
    assert due is not None
    assert due.chat_id == 1


@pytest.mark.asyncio
async def test_reserve_supports_legacy_queue_payload():
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    service = NotificationService(redis_client)
    await redis_client.rpush(
        "notifications",
        '{"chat_id": 1, "text": "from old producer"}',
    )

    reserved = await service.reserve()

    assert reserved is not None
    assert reserved.notification == Notification(
        chat_id=1,
        text="from old producer",
    )
    assert reserved.attempts == 0


@pytest.mark.asyncio
async def test_recover_processing_preserves_fifo_order():
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    service = NotificationService(redis_client)

    await service.enqueue(1, "first")
    await service.enqueue(2, "second")
    first = await service.reserve()
    second = await service.reserve()
    assert first is not None
    assert second is not None

    assert await service.recover_processing() == 2
    recovered_first = await service.reserve()
    recovered_second = await service.reserve()

    assert recovered_first is not None
    assert recovered_second is not None
    assert recovered_first.notification == first.notification
    assert recovered_second.notification == second.notification


@pytest.mark.asyncio
async def test_malformed_notification_moves_to_failed_queue():
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    service = NotificationService(redis_client)
    await redis_client.rpush("notifications", "not-json")

    assert await service.reserve() is None
    assert await redis_client.llen("notifications:processing") == 0
    assert await redis_client.llen("notifications:failed") == 1
