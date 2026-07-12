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
