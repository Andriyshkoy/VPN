from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time

from aiogram import Bot, Dispatcher
from aiogram.types import Update

from core.config import settings
from core.services.telegram_updates import (
    ClaimedTelegramUpdate,
    TelegramUpdateService,
)
from core.services.telegram_user_actions import TelegramActionAuditContext

logger = logging.getLogger(__name__)


class _TelegramUpdateLeaseLost(RuntimeError):
    """The current processor can no longer prove ownership of an update."""


class _TelegramUpdateHeartbeatFailed(RuntimeError):
    """Lease renewal failed before ownership could be proven."""


class TelegramPollingIngestor:
    """Persist long-poll responses before advancing Telegram's ACK offset."""

    def __init__(
        self,
        bot: Bot,
        service: TelegramUpdateService,
        *,
        allowed_updates: list[str] | None = None,
    ) -> None:
        self.bot = bot
        self.service = service
        self.allowed_updates = allowed_updates
        self.offset: int | None = None

    async def poll_once(self) -> int:
        updates = await self.bot.get_updates(
            offset=self.offset,
            limit=settings.telegram_update_batch_size,
            timeout=settings.telegram_update_poll_timeout,
            allowed_updates=self.allowed_updates,
            request_timeout=settings.telegram_update_poll_timeout + 10,
        )
        payloads = [
            update.model_dump(mode="json", exclude_none=True) for update in updates
        ]

        # This commit is the acknowledgement boundary. If it fails, ``offset``
        # remains unchanged and Telegram returns the same response again.
        await self.service.ingest_many(payloads, source="polling")
        if updates:
            self.offset = max(update.update_id for update in updates) + 1
        return len(updates)

    async def run(self) -> None:
        retry_delay = 1.0
        while True:
            try:
                await self.poll_once()
                retry_delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Telegram polling/ingestion failed; ACK offset was not advanced"
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30.0)


class TelegramUpdateProcessor:
    """Feed durably claimed updates into the existing aiogram dispatcher."""

    def __init__(
        self,
        bot: Bot,
        dispatcher: Dispatcher,
        service: TelegramUpdateService,
        *,
        cleanup_enabled: bool = True,
    ) -> None:
        self.bot = bot
        self.dispatcher = dispatcher
        self.service = service
        self.cleanup_enabled = cleanup_enabled
        self._next_cleanup_at = 0.0

    async def process_one(self) -> bool:
        claimed = await self.service.claim_next()
        if claimed is None:
            return False

        audit_context = TelegramActionAuditContext.from_payload(claimed.payload)
        handler = asyncio.create_task(self._dispatch(claimed, audit_context))
        heartbeat = asyncio.create_task(self._renew_lease(claimed))
        try:
            done, _ = await asyncio.wait(
                {handler, heartbeat},
                timeout=settings.telegram_update_handler_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if not done:
                timeout = TimeoutError(
                    "Telegram update handler exceeded its fail-safe timeout"
                )
                await self._cancel_handler(handler)
                await self._stop_heartbeat(heartbeat)
                await self.service.mark_failed(
                    claimed, timeout, audit_context=audit_context
                )
                logger.error(
                    "Telegram update %s handler timed out on attempt %s",
                    claimed.update_id,
                    claimed.attempts,
                )
                return True

            # Lease ownership wins the race over handler completion. If both
            # tasks finish together, fail closed and never ACK using a lease we
            # can no longer prove we own.
            if heartbeat in done:
                try:
                    heartbeat.result()
                except _TelegramUpdateLeaseLost:
                    logger.warning(
                        "Telegram update %s lost its processing lease; "
                        "cancelling the handler",
                        claimed.update_id,
                    )
                except _TelegramUpdateHeartbeatFailed:
                    logger.exception(
                        "Telegram update %s lease heartbeat failed; "
                        "cancelling the handler",
                        claimed.update_id,
                    )
                else:  # pragma: no cover - heartbeat is intentionally endless
                    logger.error(
                        "Telegram update %s heartbeat stopped unexpectedly",
                        claimed.update_id,
                    )
                await self._cancel_handler(handler)
                return True

            await self._stop_heartbeat(heartbeat)
            try:
                handler.result()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "Telegram update %s failed on attempt %s",
                    claimed.update_id,
                    claimed.attempts,
                    exc_info=exc,
                )
                await self.service.mark_failed(
                    claimed, exc, audit_context=audit_context
                )
            else:
                completed = await self.service.mark_processed(
                    claimed, audit_context=audit_context
                )
                if not completed:
                    logger.warning(
                        "Telegram update %s lost its processing lease before ACK",
                        claimed.update_id,
                    )
            return True
        except asyncio.CancelledError:
            # Do not ACK on shutdown. The renewable lease expires and another
            # process resumes this update after restart.
            raise
        finally:
            await self._cancel_handler(handler)
            await self._stop_heartbeat(heartbeat)

    async def _dispatch(
        self,
        claimed: ClaimedTelegramUpdate,
        audit_context: TelegramActionAuditContext,
    ) -> None:
        update = Update.model_validate(
            claimed.payload,
            context={"bot": self.bot},
        )
        parameters = inspect.signature(self.dispatcher.feed_update).parameters.values()
        supports_workflow_data = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            or parameter.name == "telegram_action_audit"
            for parameter in parameters
        )
        if supports_workflow_data:
            await self.dispatcher.feed_update(
                self.bot,
                update,
                telegram_action_audit=audit_context,
            )
        else:
            # Lightweight dispatcher test doubles and legacy adapters may not
            # expose aiogram's workflow-data kwargs. The central safe fallback
            # remains available even without handler outcome enrichment.
            await self.dispatcher.feed_update(self.bot, update)

    async def run(self) -> None:
        retry_delay = 1.0
        while True:
            try:
                processed = await self.process_one()
                retry_delay = 1.0
                if self.cleanup_enabled:
                    await self._purge_old_updates_if_due()
                if not processed:
                    await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A database failure leaves the lease intact. It becomes
                # claimable after expiry, so retrying the loop cannot lose it.
                logger.exception("Durable Telegram update processor failed")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30.0)

    async def _purge_old_updates_if_due(self) -> None:
        now = time.monotonic()
        if now < self._next_cleanup_at:
            return
        await self.service.purge_terminal()
        self._next_cleanup_at = now + 3600

    async def _renew_lease(self, claimed: ClaimedTelegramUpdate) -> None:
        interval = max(1, settings.telegram_update_lease_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self.service.renew_lease(claimed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise _TelegramUpdateHeartbeatFailed(
                    f"Could not renew Telegram update {claimed.update_id} lease"
                ) from exc
            if not renewed:
                raise _TelegramUpdateLeaseLost(
                    f"Telegram update {claimed.update_id} lease is no longer owned"
                )

    @staticmethod
    async def _cancel_handler(task: asyncio.Task) -> None:
        if not task.done():
            task.cancel()
        # The normal result/exception is consumed above when the handler wins
        # the race. Cleanup must not replay an already handled exception.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    @staticmethod
    async def _stop_heartbeat(task: asyncio.Task) -> None:
        if not task.done():
            task.cancel()
        with contextlib.suppress(
            asyncio.CancelledError,
            _TelegramUpdateLeaseLost,
            _TelegramUpdateHeartbeatFailed,
        ):
            await task
