from __future__ import annotations

import asyncio
import contextlib
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Sequence
from uuid import uuid4

import redis.asyncio as redis
from redis.exceptions import WatchError

from core.config import settings


@dataclass
class Notification:
    """Simple representation of a pending Telegram message."""

    chat_id: int
    text: str


@dataclass(frozen=True)
class ReservedNotification:
    """A notification owned by a consumer until it is acknowledged."""

    notification: Notification
    receipt: str
    notification_id: str
    attempts: int = 0
    created_at: float = 0
    available_at: float = 0

    @property
    def chat_id(self) -> int:
        return self.notification.chat_id

    @property
    def text(self) -> str:
        return self.notification.text


class NotificationService:
    """Redis-backed at-least-once queue for notifications.

    Consumers atomically move an item from the pending list to a processing list.
    The item remains there until it is acknowledged or explicitly retried.  This
    prevents a bot crash between reading a notification and sending it from
    silently losing the message.
    """

    def __init__(
        self,
        redis_client: redis.Redis | None = None,
        *,
        key: str = "notifications",
    ) -> None:
        self._redis = redis_client or redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        self._key = key
        self._processing_key = f"{key}:processing"
        self._failed_key = f"{key}:failed"
        self._listener_token: str | None = None

    async def enqueue(
        self,
        chat_id: int,
        text: str,
        *,
        notification_id: str | None = None,
    ) -> bool:
        """Deduplicate a stable outbox ID for one visibility window."""

        supplied_id = notification_id is not None
        notification_id = notification_id or uuid4().hex
        if not notification_id or len(notification_id) > 160:
            raise ValueError("invalid notification ID")
        data = self._serialize(
            notification_id=notification_id,
            notification=Notification(chat_id=chat_id, text=text),
            attempts=0,
            created_at=time.time(),
            available_at=0,
        )
        if not supplied_id:
            await self._redis.rpush(self._key, data)
            return True

        # SET marker + RPUSH are committed in the same Redis transaction.  If
        # PostgreSQL commits the outbox row but a publisher crashes, retrying the
        # same stable ID cannot enqueue a second copy.
        marker_key = f"{self._key}:enqueued:{notification_id}"
        while True:
            async with self._redis.pipeline(transaction=True) as pipeline:
                try:
                    await pipeline.watch(marker_key)
                    if await pipeline.exists(marker_key):
                        await pipeline.unwatch()
                        return False
                    pipeline.multi()
                    pipeline.set(
                        marker_key,
                        "1",
                        ex=settings.notification_dedupe_ttl_seconds,
                    )
                    pipeline.rpush(self._key, data)
                    await pipeline.execute()
                    return True
                except WatchError:
                    continue

    @asynccontextmanager
    async def listener_lock(self) -> AsyncIterator[bool]:
        """Elect one notification consumer, including during rolling restarts."""

        timeout = settings.notification_visibility_timeout
        lock_key = f"{self._key}:listener-lock"
        token = uuid4().hex
        acquired = bool(
            await self._redis.set(
                lock_key,
                token,
                nx=True,
                ex=timeout,
            )
        )
        if acquired:
            self._listener_token = token
        heartbeat: asyncio.Task | None = None

        async def compare_and_expire() -> bool:
            while True:
                async with self._redis.pipeline(transaction=True) as pipeline:
                    try:
                        await pipeline.watch(lock_key)
                        current = await pipeline.get(lock_key)
                        if current is None or self._as_text(current) != token:
                            await pipeline.unwatch()
                            return False
                        pipeline.multi()
                        pipeline.expire(lock_key, timeout)
                        await pipeline.execute()
                        return True
                    except WatchError:
                        continue

        async def compare_and_delete() -> None:
            while True:
                async with self._redis.pipeline(transaction=True) as pipeline:
                    try:
                        await pipeline.watch(lock_key)
                        current = await pipeline.get(lock_key)
                        if current is None or self._as_text(current) != token:
                            await pipeline.unwatch()
                            return
                        pipeline.multi()
                        pipeline.delete(lock_key)
                        await pipeline.execute()
                        return
                    except WatchError:
                        continue

        async def extend_lease() -> None:
            while True:
                await asyncio.sleep(max(1, timeout // 3))
                try:
                    extended = await compare_and_expire()
                except Exception:
                    self._listener_token = None
                    return
                if not extended:
                    self._listener_token = None
                    return

        if acquired:
            heartbeat = asyncio.create_task(extend_lease())
        try:
            yield acquired
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
            if acquired and self._listener_token == token:
                try:
                    await compare_and_delete()
                finally:
                    self._listener_token = None

    async def listener_lease_is_valid(self) -> bool:
        """Fail closed when Redis cannot prove this consumer still owns lease."""

        token = self._listener_token
        if token is None:
            return False
        try:
            current = await self._redis.get(f"{self._key}:listener-lock")
        except Exception:
            self._listener_token = None
            return False
        valid = current is not None and self._as_text(current) == token
        if not valid:
            self._listener_token = None
        return valid

    async def pending_count(self) -> int:
        """Return the number of notifications waiting to be reserved."""
        return int(await self._redis.llen(self._key))

    async def reserve(self) -> ReservedNotification | None:
        """Atomically reserve the oldest pending notification.

        Malformed queue entries are moved to the failed list so one corrupt item
        cannot permanently stop delivery of later notifications.
        """
        # Scan one snapshot so delayed retries cannot create a hot loop or block
        # ready messages queued behind them.
        scan_count = await self.pending_count()
        for _ in range(scan_count):
            raw = await self._redis.lmove(
                self._key,
                self._processing_key,
                "LEFT",
                "RIGHT",
            )
            if raw is None:
                return None

            receipt = self._as_text(raw)
            try:
                reserved = self._deserialize(receipt)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                await self._fail_raw(receipt, reason=f"invalid payload: {exc}")
                continue
            if reserved.available_at > time.time():
                await self._replace_processing_item(
                    receipt=receipt,
                    destination=self._key,
                    payload=receipt,
                )
                continue
            return reserved
        return None

    async def acknowledge(self, reserved: ReservedNotification) -> bool:
        """Remove a successfully handled reservation from the processing list."""
        removed = await self._redis.lrem(
            self._processing_key,
            1,
            reserved.receipt,
        )
        return bool(removed)

    async def retry(
        self,
        reserved: ReservedNotification,
        *,
        delay_seconds: float = 0,
    ) -> bool:
        """Atomically replace a reservation with a new pending attempt."""

        if delay_seconds < 0:
            raise ValueError("retry delay must not be negative")
        payload = self._serialize(
            notification_id=reserved.notification_id,
            notification=reserved.notification,
            attempts=reserved.attempts + 1,
            created_at=reserved.created_at,
            available_at=time.time() + delay_seconds,
        )
        moved = await self._replace_processing_item(
            receipt=reserved.receipt,
            destination=self._key,
            payload=payload,
        )
        if moved:
            await self._redis.expire(
                f"{self._key}:enqueued:{reserved.notification_id}",
                settings.notification_dedupe_ttl_seconds,
            )
        return moved

    async def fail(
        self,
        reserved: ReservedNotification,
        *,
        reason: str,
        category: str | None = None,
    ) -> bool:
        """Move a terminally failed reservation to the diagnostic failed list."""
        payload = json.dumps(
            {
                "notification": json.loads(reserved.receipt),
                "reason": reason,
                "category": category,
            },
            ensure_ascii=False,
        )
        return await self._replace_processing_item(
            receipt=reserved.receipt,
            destination=self._failed_key,
            payload=payload,
        )

    async def recover_processing(self) -> int:
        """Return reservations left by an interrupted consumer to the queue.

        The current deployment has one notification listener.  Calling this on
        listener startup (and after Redis errors) restores its unfinished work
        while preserving FIFO order.
        """
        recovered = 0
        count = int(await self._redis.llen(self._processing_key))
        for _ in range(count):
            raw = await self._redis.lmove(
                self._processing_key,
                self._key,
                "RIGHT",
                "LEFT",
            )
            if raw is None:
                break
            recovered += 1
        return recovered

    async def get_pending(self) -> Sequence[Notification]:
        """Return and acknowledge all queued notifications.

        This compatibility API retains the old destructive-drain contract for
        callers that do not support reservations.  Reliable consumers should use
        :meth:`reserve` followed by :meth:`acknowledge`, :meth:`retry`, or
        :meth:`fail`.
        """
        notifications: list[Notification] = []
        count = await self.pending_count()
        for _ in range(count):
            reserved = await self.reserve()
            if reserved is None:
                break
            notifications.append(reserved.notification)
            await self.acknowledge(reserved)
        return notifications

    async def _fail_raw(self, receipt: str, *, reason: str) -> bool:
        payload = json.dumps(
            {"notification": receipt, "reason": reason},
            ensure_ascii=False,
        )
        return await self._replace_processing_item(
            receipt=receipt,
            destination=self._failed_key,
            payload=payload,
        )

    async def _replace_processing_item(
        self,
        *,
        receipt: str,
        destination: str,
        payload: str,
    ) -> bool:
        """Move a specific processing item using Redis optimistic locking."""
        while True:
            async with self._redis.pipeline(transaction=True) as pipeline:
                try:
                    await pipeline.watch(self._processing_key)
                    processing = await pipeline.lrange(
                        self._processing_key,
                        0,
                        -1,
                    )
                    if receipt not in map(self._as_text, processing):
                        await pipeline.unwatch()
                        return False

                    pipeline.multi()
                    pipeline.lrem(self._processing_key, 1, receipt)
                    pipeline.rpush(destination, payload)
                    await pipeline.execute()
                    return True
                except WatchError:
                    continue

    @staticmethod
    def _serialize(
        *,
        notification_id: str,
        notification: Notification,
        attempts: int,
        created_at: float,
        available_at: float,
    ) -> str:
        return json.dumps(
            {
                "id": notification_id,
                "chat_id": notification.chat_id,
                "text": notification.text,
                "attempts": attempts,
                "created_at": created_at,
                "available_at": available_at,
            },
            ensure_ascii=False,
        )

    @classmethod
    def _deserialize(cls, receipt: str) -> ReservedNotification:
        item = json.loads(receipt)
        if not isinstance(item, dict):
            raise TypeError("notification payload must be an object")

        # Records created before reliable delivery did not have an id/attempts.
        notification_id = str(item.get("id") or uuid4().hex)
        attempts = int(item.get("attempts", 0))
        if attempts < 0:
            raise ValueError("attempts must not be negative")
        created_at = float(item.get("created_at", time.time()))
        available_at = float(item.get("available_at", 0))
        if created_at < 0 or available_at < 0:
            raise ValueError("notification timestamps must not be negative")

        text = item["text"]
        if not isinstance(text, str):
            raise TypeError("notification text must be a string")

        return ReservedNotification(
            notification=Notification(chat_id=int(item["chat_id"]), text=text),
            receipt=receipt,
            notification_id=notification_id,
            attempts=attempts,
            created_at=created_at,
            available_at=available_at,
        )

    @staticmethod
    def _as_text(value: str | bytes) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return value
