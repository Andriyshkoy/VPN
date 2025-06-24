from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Sequence

import redis.asyncio as redis

from core.config import settings


@dataclass
class Notification:
    """Simple representation of a pending Telegram message."""

    chat_id: int
    text: str


class NotificationService:
    """Redis-backed queue for notifications."""

    def __init__(
        self,
        redis_client: redis.Redis | None = None,
        *,
        key: str = "notifications",
    ) -> None:
        self._redis = redis_client or redis.from_url(
            settings.redis_url, decode_responses=True
        )
        self._key = key

    async def enqueue(self, chat_id: int, text: str) -> None:
        """Add a new notification to the queue."""
        data = json.dumps({"chat_id": chat_id, "text": text})
        await self._redis.rpush(self._key, data)

    async def get_pending(self) -> Sequence[Notification]:
        """Return and remove all queued notifications."""
        raw = await self._redis.lrange(self._key, 0, -1)
        if raw:
            await self._redis.delete(self._key)
        return [
            Notification(chat_id=int(item["chat_id"]), text=item["text"])
            for item in map(json.loads, raw)
        ]
