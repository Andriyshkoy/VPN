import pytest
import fakeredis.aioredis

from core.services.notifications import NotificationService


@pytest.mark.asyncio
async def test_notifications_queue_roundtrip(sessionmaker):
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    service = NotificationService(redis_client=redis_client, key="test_notifications")

    await service.enqueue(1, "hello")
    await service.enqueue(2, "world")

    pending = await service.get_pending()
    assert [n.chat_id for n in pending] == [1, 2]
    assert [n.text for n in pending] == ["hello", "world"]

    pending_again = await service.get_pending()
    assert pending_again == []
