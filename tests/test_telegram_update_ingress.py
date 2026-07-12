from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from aiogram.types import Update
from pydantic import ValidationError

from bot.update_ingress import (
    TelegramPollingIngestor,
    TelegramUpdateProcessor,
    _TelegramUpdateHeartbeatFailed,
    _TelegramUpdateLeaseLost,
)
from core.config import Settings, settings
from core.db.unit_of_work import uow
from core.domain.telegram import TelegramUpdateStatus
from core.services.telegram_updates import TelegramUpdateService


def test_update_handler_timeout_and_dead_retention_must_be_fail_safe():
    common = {
        "database_url": "sqlite+aiosqlite:///:memory:",
        "encryption_key": settings.encryption_key,
        "_env_file": None,
    }
    with pytest.raises(ValidationError, match="handler timeout"):
        Settings(
            **common,
            telegram_update_lease_seconds=60,
            telegram_update_handler_timeout_seconds=60,
        )
    with pytest.raises(ValidationError, match="dead-update retention"):
        Settings(
            **common,
            telegram_update_retention_days=7,
            telegram_update_dead_retention_days=7,
        )


@pytest.mark.asyncio
async def test_ingestion_is_idempotent_and_keeps_first_payload(sessionmaker):
    service = TelegramUpdateService(uow)

    assert await service.ingest({"update_id": 42, "message": {"text": "first"}})
    assert not await service.ingest(
        {"update_id": 42, "message": {"text": "replacement"}},
        source="webhook",
    )

    async with uow() as repos:
        rows = await repos.telegram_updates.list()
        assert len(rows) == 1
        assert rows[0].source == "polling"
        assert rows[0].payload["message"]["text"] == "first"


@pytest.mark.asyncio
async def test_ingest_batch_validates_before_writing(sessionmaker):
    service = TelegramUpdateService(uow)

    with pytest.raises(ValueError):
        await service.ingest_many(({"update_id": 1}, {"update_id": "bad"}))

    async with uow() as repos:
        assert await repos.telegram_updates.list() == []


@pytest.mark.asyncio
async def test_processing_completion_is_fenced_by_lease_token(sessionmaker):
    service = TelegramUpdateService(uow)
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    await service.ingest_many(({"update_id": 2}, {"update_id": 1}))

    claimed = await service.claim_next(now=now)
    assert claimed is not None
    assert claimed.update_id == 1
    stale = replace(claimed, lease_token="stale-token")

    assert not await service.renew_lease(stale, now=now)
    assert not await service.mark_processed(stale, now=now)
    assert await service.mark_processed(claimed, now=now)

    async with uow() as repos:
        row = await repos.telegram_updates.get(update_id=1)
        assert row.status == TelegramUpdateStatus.PROCESSED.value
        assert row.processed_at is not None
        assert row.payload == {}


@pytest.mark.asyncio
async def test_expired_processing_lease_is_recovered_and_old_worker_is_fenced(
    sessionmaker,
):
    service = TelegramUpdateService(uow)
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    await service.ingest({"update_id": 7})

    first = await service.claim_next(now=now)
    assert first is not None
    assert await service.claim_next(now=now + timedelta(seconds=299)) is None

    recovered = await service.claim_next(now=now + timedelta(seconds=301))
    assert recovered is not None
    assert recovered.update_id == first.update_id
    assert recovered.attempts == 2
    assert recovered.lease_token != first.lease_token
    assert not await service.mark_processed(first, now=now + timedelta(seconds=301))
    assert await service.mark_processed(recovered, now=now + timedelta(seconds=301))


@pytest.mark.asyncio
async def test_failed_update_retries_with_backoff_then_moves_to_dead_letter(
    sessionmaker,
    monkeypatch,
):
    monkeypatch.setattr(settings, "telegram_update_max_attempts", 2)
    service = TelegramUpdateService(uow)
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    await service.ingest({"update_id": 9})

    first = await service.claim_next(now=now)
    assert first is not None
    assert await service.mark_failed(first, RuntimeError("temporary"), now=now)
    assert await service.claim_next(now=now) is None

    second = await service.claim_next(now=now + timedelta(seconds=1))
    assert second is not None
    assert second.attempts == 2
    assert await service.mark_failed(
        second,
        RuntimeError("poison update"),
        now=now + timedelta(seconds=1),
    )
    assert await service.claim_next(now=now + timedelta(days=1)) is None

    async with uow() as repos:
        row = await repos.telegram_updates.get(update_id=9)
        assert row.status == TelegramUpdateStatus.DEAD.value
        assert "poison update" in row.last_error


@pytest.mark.asyncio
async def test_failed_head_blocks_only_its_conversation_lane(sessionmaker):
    service = TelegramUpdateService(uow)
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    await service.ingest_many(
        (
            {
                "update_id": 30,
                "message": {"chat": {"id": 100}, "from": {"id": 200}},
            },
            {
                "update_id": 31,
                "callback_query": {
                    "from": {"id": 200},
                    "message": {"chat": {"id": 100}},
                },
            },
            {
                "update_id": 32,
                "message": {"chat": {"id": 300}, "from": {"id": 400}},
            },
        )
    )

    head = await service.claim_next(now=now)
    assert head is not None and head.update_id == 30
    assert await service.mark_failed(head, RuntimeError("retry head"), now=now)

    # A different conversation remains available while update 31 cannot
    # overtake failed update 30 in the same Redis FSM lane.
    unrelated = await service.claim_next(now=now)
    assert unrelated is not None and unrelated.update_id == 32
    assert await service.mark_processed(unrelated, now=now)
    assert await service.claim_next(now=now) is None

    retried = await service.claim_next(now=now + timedelta(seconds=1))
    assert retried is not None and retried.update_id == 30
    assert await service.mark_processed(retried, now=now + timedelta(seconds=1))

    follower = await service.claim_next(now=now + timedelta(seconds=1))
    assert follower is not None and follower.update_id == 31

    async with uow() as repos:
        first = await repos.telegram_updates.get(update_id=30)
        second = await repos.telegram_updates.get(update_id=31)
        third = await repos.telegram_updates.get(update_id=32)
        assert first.ordering_key == second.ordering_key
        assert first.ordering_key != third.ordering_key
        assert first.ordering_key.startswith("v1:")
        assert len(first.ordering_key) == 67


@pytest.mark.asyncio
async def test_lowered_retry_budget_terminalizes_failed_lane_head(
    sessionmaker,
    monkeypatch,
):
    monkeypatch.setattr(settings, "telegram_update_max_attempts", 3)
    service = TelegramUpdateService(uow)
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    await service.ingest_many(
        (
            _message_update(33, chat_id=330, user_id=330),
            _message_update(34, chat_id=330, user_id=330),
        )
    )
    head = await service.claim_next(now=now)
    assert head is not None and head.update_id == 33
    assert await service.mark_failed(head, RuntimeError("keep this error"), now=now)

    monkeypatch.setattr(settings, "telegram_update_max_attempts", 1)
    follower = await service.claim_next(now=now + timedelta(seconds=1))
    assert follower is not None and follower.update_id == 34
    async with uow() as repos:
        terminal = await repos.telegram_updates.get(update_id=33)
        assert terminal.status == TelegramUpdateStatus.DEAD.value
        assert "keep this error" in terminal.last_error


@pytest.mark.asyncio
async def test_crash_during_final_attempt_moves_to_dead_after_lease(
    sessionmaker,
    monkeypatch,
):
    monkeypatch.setattr(settings, "telegram_update_max_attempts", 1)
    service = TelegramUpdateService(uow)
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    await service.ingest({"update_id": 19})

    assert await service.claim_next(now=now) is not None
    assert await service.claim_next(now=now + timedelta(seconds=301)) is None

    async with uow() as repos:
        row = await repos.telegram_updates.get(update_id=19)
        assert row.status == TelegramUpdateStatus.DEAD.value
        assert row.last_error == "processing lease expired after final attempt"


@pytest.mark.asyncio
async def test_retention_deletes_old_processed_and_shorter_lived_dead_updates(
    sessionmaker,
    monkeypatch,
):
    monkeypatch.setattr(settings, "telegram_update_retention_days", 30)
    monkeypatch.setattr(settings, "telegram_update_dead_retention_days", 7)
    monkeypatch.setattr(settings, "telegram_update_max_attempts", 1)
    service = TelegramUpdateService(uow)
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    await service.ingest_many(
        (
            {"update_id": 20},
            {"update_id": 21},
            {"update_id": 22},
            {"update_id": 23},
        )
    )

    processed = await service.claim_next(now=now)
    assert processed is not None
    assert await service.mark_processed(processed, now=now - timedelta(days=31))

    old_dead = await service.claim_next(now=now)
    assert old_dead is not None
    assert await service.mark_failed(
        old_dead,
        RuntimeError("old poison update"),
        now=now - timedelta(days=8),
    )

    recent_dead = await service.claim_next(now=now)
    assert recent_dead is not None
    assert await service.mark_failed(
        recent_dead,
        RuntimeError("recent poison update"),
        now=now - timedelta(days=1),
    )

    assert await service.purge_terminal(now=now) == 2

    async with uow() as repos:
        assert await repos.telegram_updates.get(update_id=20) is None
        assert await repos.telegram_updates.get(update_id=21) is None
        recent = await repos.telegram_updates.get(update_id=22)
        assert recent.status == TelegramUpdateStatus.DEAD.value
        pending = await repos.telegram_updates.get(update_id=23)
        assert pending.status == TelegramUpdateStatus.PENDING.value


class _PollingBot:
    def __init__(self, updates):
        self.updates = updates
        self.offsets = []

    async def get_updates(self, **kwargs):
        self.offsets.append(kwargs["offset"])
        return self.updates


class _IngestionService:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.payloads = []

    async def ingest_many(self, payloads, *, source):
        self.payloads.append((payloads, source))
        if self.fail:
            raise ConnectionError("database unavailable")
        return len(payloads)


@pytest.mark.asyncio
async def test_polling_advances_offset_only_after_database_commit():
    bot = _PollingBot([Update(update_id=100), Update(update_id=101)])
    service = _IngestionService(fail=True)
    ingestor = TelegramPollingIngestor(bot, service)

    with pytest.raises(ConnectionError):
        await ingestor.poll_once()
    assert ingestor.offset is None

    service.fail = False
    assert await ingestor.poll_once() == 2
    assert bot.offsets == [None, None]
    assert ingestor.offset == 102
    assert service.payloads[-1][0][0] == {"update_id": 100}


class _Dispatcher:
    def __init__(self, error=None):
        self.error = error
        self.seen = []

    async def feed_update(self, bot, update):
        self.seen.append(update.update_id)
        if self.error:
            raise self.error


class _BlockingDispatcher:
    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = False

    async def feed_update(self, bot, update):
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled = True


class _ConcurrentDispatcher:
    def __init__(self, expected: int):
        self.expected = expected
        self.seen: list[int] = []
        self.first_started = asyncio.Event()
        self.all_started = asyncio.Event()
        self.release = asyncio.Event()

    async def feed_update(self, bot, update):
        self.seen.append(update.update_id)
        self.first_started.set()
        if len(self.seen) >= self.expected:
            self.all_started.set()
        await self.release.wait()


def _message_update(update_id: int, *, chat_id: int, user_id: int) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": chat_id, "type": "private"},
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": "Test",
            },
            "text": "test",
        },
    }


@pytest.mark.asyncio
async def test_processor_acks_only_after_existing_dispatcher_succeeds(sessionmaker):
    service = TelegramUpdateService(uow)
    await service.ingest({"update_id": 55})
    dispatcher = _Dispatcher()
    processor = TelegramUpdateProcessor(object(), dispatcher, service)

    assert await processor.process_one()
    assert dispatcher.seen == [55]
    async with uow() as repos:
        row = await repos.telegram_updates.get(update_id=55)
        assert row.status == TelegramUpdateStatus.PROCESSED.value
        assert row.payload == {}


@pytest.mark.asyncio
async def test_processor_pool_dispatches_different_lanes_concurrently(sessionmaker):
    service = TelegramUpdateService(uow)
    await service.ingest_many(
        (
            _message_update(60, chat_id=600, user_id=600),
            _message_update(61, chat_id=610, user_id=610),
        )
    )
    dispatcher = _ConcurrentDispatcher(expected=2)
    first = TelegramUpdateProcessor(object(), dispatcher, service)
    second = TelegramUpdateProcessor(
        object(), dispatcher, service, cleanup_enabled=False
    )

    first_task = asyncio.create_task(first.process_one())
    await asyncio.wait_for(dispatcher.first_started.wait(), timeout=1)
    second_task = asyncio.create_task(second.process_one())
    tasks = [first_task, second_task]
    await asyncio.wait_for(dispatcher.all_started.wait(), timeout=1)
    assert set(dispatcher.seen) == {60, 61}
    dispatcher.release.set()
    assert await asyncio.gather(*tasks) == [True, True]


@pytest.mark.asyncio
async def test_processor_pool_never_overlaps_one_conversation_lane(sessionmaker):
    service = TelegramUpdateService(uow)
    await service.ingest_many(
        (
            _message_update(62, chat_id=620, user_id=620),
            _message_update(63, chat_id=620, user_id=620),
        )
    )
    dispatcher = _ConcurrentDispatcher(expected=1)
    first = TelegramUpdateProcessor(object(), dispatcher, service)
    second = TelegramUpdateProcessor(
        object(), dispatcher, service, cleanup_enabled=False
    )

    active = asyncio.create_task(first.process_one())
    await asyncio.wait_for(dispatcher.all_started.wait(), timeout=1)
    assert dispatcher.seen == [62]
    assert await second.process_one() is False

    dispatcher.release.set()
    assert await active is True
    assert await second.process_one() is True
    assert dispatcher.seen == [62, 63]


@pytest.mark.asyncio
async def test_processor_keeps_handler_failure_for_retry(sessionmaker):
    service = TelegramUpdateService(uow)
    await service.ingest({"update_id": 56})
    processor = TelegramUpdateProcessor(
        object(),
        _Dispatcher(RuntimeError("handler crashed")),
        service,
    )

    assert await processor.process_one()
    async with uow() as repos:
        row = await repos.telegram_updates.get(update_id=56)
        assert row.status == TelegramUpdateStatus.FAILED.value
        assert row.attempts == 1
        assert "handler crashed" in row.last_error


@pytest.mark.asyncio
async def test_processor_timeout_cancels_handler_before_retry(
    sessionmaker,
    monkeypatch,
):
    monkeypatch.setattr(settings, "telegram_update_handler_timeout_seconds", 0.01)
    service = TelegramUpdateService(uow)
    await service.ingest({"update_id": 57})
    dispatcher = _BlockingDispatcher()
    processor = TelegramUpdateProcessor(object(), dispatcher, service)

    assert await processor.process_one()
    assert dispatcher.started.is_set()
    assert dispatcher.cancelled
    async with uow() as repos:
        row = await repos.telegram_updates.get(update_id=57)
        assert row.status == TelegramUpdateStatus.FAILED.value
        assert "TimeoutError" in row.last_error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "heartbeat_error_type",
    (_TelegramUpdateLeaseLost, _TelegramUpdateHeartbeatFailed),
)
async def test_heartbeat_loss_cancels_handler_without_stale_ack(
    sessionmaker,
    monkeypatch,
    heartbeat_error_type,
):
    service = TelegramUpdateService(uow)
    await service.ingest({"update_id": 58})
    dispatcher = _BlockingDispatcher()
    processor = TelegramUpdateProcessor(object(), dispatcher, service)

    async def lose_lease(claimed):
        await dispatcher.started.wait()
        raise heartbeat_error_type("heartbeat stopped")

    monkeypatch.setattr(processor, "_renew_lease", lose_lease)

    assert await processor.process_one()
    assert dispatcher.cancelled
    async with uow() as repos:
        row = await repos.telegram_updates.get(update_id=58)
        # No terminal write is allowed after lease ownership becomes uncertain.
        assert row.status == TelegramUpdateStatus.PROCESSING.value
        assert row.processed_at is None
