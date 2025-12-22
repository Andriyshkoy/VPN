import os

import pytest
import redis.asyncio as redis

from core.services.notifications import NotificationService


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_notification_service_with_real_redis():
    if os.getenv("INTEGRATION_TESTS") != "1":
        pytest.skip("Integration tests are disabled")

    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        pytest.skip("REDIS_URL not set")

    client = redis.from_url(redis_url, decode_responses=True)
    key = "test_notifications"
    await client.delete(key)

    service = NotificationService(redis_client=client, key=key)
    await service.enqueue(1, "hello")

    pending = await service.get_pending()
    assert len(pending) == 1
    assert pending[0].chat_id == 1
    assert pending[0].text == "hello"

    pending_again = await service.get_pending()
    assert pending_again == []

    await client.aclose()
