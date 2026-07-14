from __future__ import annotations

from datetime import datetime

from sqlalchemy.exc import IntegrityError

from core.db.models.telegram_user_action import TelegramUserActionEvent
from core.exceptions import InvalidOperationError

from .base import BaseRepo


class TelegramUserActionRepo(BaseRepo[TelegramUserActionEvent]):
    """Single append path for immutable Telegram user-action events."""

    model = TelegramUserActionEvent

    async def add(self, obj: TelegramUserActionEvent) -> TelegramUserActionEvent:
        raise InvalidOperationError(
            "Telegram user actions can only be appended through append_once"
        )

    async def delete(self, **filters) -> int:
        raise InvalidOperationError("Telegram user action events are immutable")

    async def append_once(
        self,
        *,
        user_id: int,
        source_update_id: int,
        action: str,
        result: str,
        metadata: dict,
        occurred_at: datetime,
    ) -> tuple[TelegramUserActionEvent, bool]:
        if source_update_id < 0:
            raise ValueError("source_update_id must be non-negative")
        if not action or len(action) > 96:
            raise ValueError("invalid Telegram user action")
        if result not in {
            "handled",
            "completed",
            "rejected",
            "ignored",
            "invalid",
            "unavailable",
            "failed",
        }:
            raise ValueError("invalid Telegram user action result")

        existing = await self.get(source_update_id=source_update_id)
        if existing is not None:
            return existing, False

        row = self.model(
            user_id=user_id,
            source_update_id=source_update_id,
            category="bot",
            action=action,
            result=result,
            metadata_json=dict(metadata),
            occurred_at=occurred_at,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(row)
                await self.session.flush()
        except IntegrityError:
            existing = await self.get(source_update_id=source_update_id)
            if existing is None:
                raise
            return existing, False
        return row, True
