import fakeredis.aioredis
import pytest

from bot.notifications import send_pending_notifications
from core.services.notifications import NotificationService


class DummyBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


@pytest.mark.asyncio
async def test_send_pending_notifications():
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    service = NotificationService(redis_client)

    await service.enqueue(1, "hello")
    await service.enqueue(2, "bye")

    bot = DummyBot()
    await send_pending_notifications(bot, service)

    assert bot.sent == [(1, "hello"), (2, "bye")]
    assert await redis_client.llen("notifications") == 0
