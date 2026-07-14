from __future__ import annotations

import base64
import copy
import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable
from uuid import uuid4

from core.config import settings
from core.services.telegram_user_actions import (
    TelegramActionAuditContext,
    TelegramUserActionService,
    safe_error_type,
)


@dataclass(frozen=True, slots=True)
class ClaimedTelegramUpdate:
    update_id: int
    payload: dict
    lease_token: str
    attempts: int
    received_at: datetime


class TelegramUpdateService:
    """Application service for transport-neutral Telegram update ingestion.

    Both long polling and a future authenticated webhook adapter use
    :meth:`ingest_many`. Processing is at-least-once: the unique Telegram
    ``update_id`` deduplicates ingestion while a renewable fenced lease makes a
    crashed handler recoverable.
    """

    def __init__(self, uow_factory: Callable) -> None:
        self._uow = uow_factory
        self._lease_for = timedelta(seconds=settings.telegram_update_lease_seconds)

    async def ingest(
        self,
        payload: dict,
        *,
        source: str = "polling",
    ) -> bool:
        return bool(await self.ingest_many((payload,), source=source))

    async def ingest_many(
        self,
        payloads: Iterable[dict],
        *,
        source: str = "polling",
    ) -> int:
        """Atomically persist a Telegram response batch before it is ACKed."""

        if source not in {"polling", "webhook"}:
            raise ValueError("unsupported Telegram update source")

        normalized = [self._validate_payload(payload) for payload in payloads]
        if not normalized:
            return 0

        inserted = 0
        async with self._uow() as repos:
            for update_id, payload, ordering_key in normalized:
                _, created = await repos.telegram_updates.ingest(
                    update_id=update_id,
                    payload=payload,
                    source=source,
                    ordering_key=ordering_key,
                )
                inserted += int(created)
        return inserted

    async def claim_next(
        self,
        *,
        now: datetime | None = None,
    ) -> ClaimedTelegramUpdate | None:
        now = now or datetime.now(timezone.utc)
        lease_token = str(uuid4())
        async with self._uow() as repos:
            terminalized = await repos.telegram_updates.terminalize_exhausted(
                now=now,
                max_attempts=settings.telegram_update_max_attempts,
            )
            for dead, terminal_reason in terminalized:
                await TelegramUserActionService.append_in_transaction(
                    repos,
                    source_update_id=dead.update_id,
                    payload=dead.payload,
                    result="failed",
                    occurred_at=dead.received_at,
                    failure_metadata={
                        "attempts": dead.attempts,
                        "error_type": terminal_reason,
                    },
                )
                # Classification is complete; dead updates never need replay.
                dead.payload = {}
            row = await repos.telegram_updates.claim_next(
                lease_token=lease_token,
                now=now,
                lease_for=self._lease_for,
                max_attempts=settings.telegram_update_max_attempts,
            )
            if row is None:
                return None
            return ClaimedTelegramUpdate(
                update_id=row.update_id,
                payload=row.payload,
                lease_token=lease_token,
                attempts=row.attempts,
                received_at=row.received_at,
            )

    async def renew_lease(
        self,
        claimed: ClaimedTelegramUpdate,
        *,
        now: datetime | None = None,
    ) -> bool:
        now = now or datetime.now(timezone.utc)
        async with self._uow() as repos:
            return await repos.telegram_updates.renew_lease(
                claimed.update_id,
                lease_token=claimed.lease_token,
                now=now,
                lease_for=self._lease_for,
            )

    async def mark_processed(
        self,
        claimed: ClaimedTelegramUpdate,
        *,
        now: datetime | None = None,
        audit_context: TelegramActionAuditContext | None = None,
    ) -> bool:
        now = now or datetime.now(timezone.utc)
        async with self._uow() as repos:
            completed = await repos.telegram_updates.mark_processed(
                claimed.update_id,
                lease_token=claimed.lease_token,
                now=now,
            )
            if completed:
                await TelegramUserActionService.append_in_transaction(
                    repos,
                    source_update_id=claimed.update_id,
                    payload=claimed.payload,
                    result="handled",
                    occurred_at=claimed.received_at,
                    audit_context=audit_context,
                )
            return completed

    async def mark_failed(
        self,
        claimed: ClaimedTelegramUpdate,
        error: Exception | str,
        *,
        now: datetime | None = None,
        audit_context: TelegramActionAuditContext | None = None,
    ) -> bool:
        now = now or datetime.now(timezone.utc)
        exhausted = claimed.attempts >= settings.telegram_update_max_attempts
        delay = min(
            2 ** max(0, claimed.attempts - 1),
            settings.telegram_update_retry_max_seconds,
        )
        async with self._uow() as repos:
            completed = await repos.telegram_updates.mark_failed(
                claimed.update_id,
                lease_token=claimed.lease_token,
                error=f"{type(error).__name__}: {error}"[:4000],
                now=now,
                next_attempt_at=now + timedelta(seconds=delay),
                exhausted=exhausted,
            )
            if completed and exhausted:
                await TelegramUserActionService.append_in_transaction(
                    repos,
                    source_update_id=claimed.update_id,
                    payload=claimed.payload,
                    result="failed",
                    occurred_at=claimed.received_at,
                    failure_metadata={
                        "attempts": claimed.attempts,
                        "error_type": safe_error_type(error),
                    },
                    audit_context=audit_context,
                )
            return completed

    async def purge_terminal(
        self,
        *,
        now: datetime | None = None,
        limit: int = 1000,
    ) -> int:
        now = now or datetime.now(timezone.utc)
        processed_cutoff = now - timedelta(days=settings.telegram_update_retention_days)
        dead_cutoff = now - timedelta(days=settings.telegram_update_dead_retention_days)
        async with self._uow() as repos:
            return await repos.telegram_updates.purge_terminal_before(
                processed_cutoff=processed_cutoff,
                dead_cutoff=dead_cutoff,
                limit=limit,
            )

    @staticmethod
    def _validate_payload(payload: dict) -> tuple[int, dict, str]:
        if not isinstance(payload, dict):
            raise ValueError("Telegram update payload must be an object")
        update_id = payload.get("update_id")
        if (
            isinstance(update_id, bool)
            or not isinstance(update_id, int)
            or update_id < 0
        ):
            raise ValueError("Telegram update_id must be a non-negative integer")
        # The inbox needs the update for replay, but handlers never use receipt
        # email/name/phone/address fields. Keep that PII out of durable storage.
        copied = copy.deepcopy(payload)
        message = copied.get("message")
        if isinstance(message, dict):
            payment = message.get("successful_payment")
            if isinstance(payment, dict):
                payment.pop("order_info", None)
        pre_checkout = copied.get("pre_checkout_query")
        if isinstance(pre_checkout, dict):
            pre_checkout.pop("order_info", None)
        shipping_query = copied.get("shipping_query")
        if isinstance(shipping_query, dict):
            shipping_query.pop("shipping_address", None)
        return update_id, copied, TelegramUpdateService._ordering_key(copied, update_id)

    @staticmethod
    def _ordering_key(payload: dict, update_id: int) -> str:
        """Derive a pseudonymous aiogram FSM lane from chat/user identity."""

        event = next(
            (
                value
                for key, value in payload.items()
                if key != "update_id" and isinstance(value, dict)
            ),
            {},
        )

        def nested_int(obj: dict, *path: str) -> int | None:
            value: object = obj
            for part in path:
                if not isinstance(value, dict):
                    return None
                value = value.get(part)
            if isinstance(value, bool) or not isinstance(value, int):
                return None
            return value

        chat_id = (
            nested_int(event, "chat", "id")
            or nested_int(event, "message", "chat", "id")
            or nested_int(event, "voter_chat", "id")
            or nested_int(event, "sender_chat", "id")
            or nested_int(event, "user_chat_id")
        )
        user_id = nested_int(event, "from", "id") or nested_int(event, "user", "id")
        if chat_id is None and user_id is not None:
            chat_id = user_id
        if user_id is None and chat_id is not None:
            user_id = chat_id
        subject = (
            f"conversation:{chat_id}:{user_id}"
            if chat_id is not None and user_id is not None
            else f"isolated-update:{update_id}"
        )
        secret = base64.urlsafe_b64decode(settings.encryption_key.encode("ascii"))
        digest = hmac.new(secret, subject.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"v1:{digest}"
