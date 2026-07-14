from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery, TelegramObject

from core.services import TelegramActionAuditContext, UserService

# The middleware only lets a syntactically valid private invitation reach the
# /start handler. The service performs the authoritative database lookup.
_INVITED_START_RE = re.compile(
    r"^/start(?:@[A-Za-z0-9_]+)?\s+ref_[A-Za-z0-9_-]{32}\s*$"
)
logger = logging.getLogger(__name__)


class InviteOnlyAccessMiddleware(BaseMiddleware):
    """Keep the bot closed while grandfathering every existing account.

    A middleware boundary is intentionally used instead of checks in separate
    handlers: callbacks and FSM catch-alls must not become alternate account
    creation paths. Telegram payment protocol events receive explicit handling
    so an unknown account is never approved at pre-checkout.
    """

    def __init__(self, user_service: UserService):
        self._user_service = user_service

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        telegram_action_audit = data.get("telegram_action_audit")
        if not isinstance(telegram_action_audit, TelegramActionAuditContext):
            telegram_action_audit = None
        from_user = getattr(event, "from_user", None)
        if from_user is None:
            return await handler(event, data)

        try:
            user = await self._user_service.find_by_tg_id(
                from_user.id,
                reactivate_delivery=self._is_private_inbound(event),
            )
        except Exception:
            if telegram_action_audit is not None:
                telegram_action_audit.record(
                    "access.invite_lookup",
                    result="unavailable",
                    metadata={"reason_code": "lookup_failed"},
                )
            # Database uncertainty must never look like a definitive access
            # denial: regular/captured updates stay retryable in the durable
            # inbox. Pre-checkout is the one protocol event that must receive
            # an immediate fail-closed answer from Telegram's point of view.
            logger.exception(
                "Invite-only access lookup failed",
                extra={"telegram_event_type": type(event).__name__},
            )
            if isinstance(event, PreCheckoutQuery):
                bot = data.get("bot")
                if bot is not None:
                    await bot.answer_pre_checkout_query(
                        event.id,
                        ok=False,
                        error_message="Не удалось проверить доступ. Попробуйте позже.",
                    )
                return None
            raise
        if user is not None:
            data["current_user"] = user
            return await handler(event, data)

        if isinstance(event, PreCheckoutQuery):
            bot = data.get("bot")
            if bot is not None:
                await bot.answer_pre_checkout_query(
                    event.id,
                    ok=False,
                    error_message="Доступ к боту возможен только по приглашению.",
                )
            if telegram_action_audit is not None:
                telegram_action_audit.record(
                    "access.invite_required",
                    result="rejected",
                    metadata={"reason_code": "invite_required"},
                )
            return None

        if isinstance(event, CallbackQuery):
            # Close the client-side spinner without disclosing account state.
            with contextlib.suppress(TelegramAPIError):
                await event.answer()
            if telegram_action_audit is not None:
                telegram_action_audit.record(
                    "access.invite_required",
                    result="rejected",
                    metadata={"reason_code": "invite_required"},
                )
            return None

        if isinstance(event, Message):
            # A captured payment cannot be rejected here. Let the payment
            # handler fail loudly instead of fabricating an unauthorized user.
            if event.successful_payment is not None:
                return await handler(event, data)
            if self._is_private_invited_start(event):
                return await handler(event, data)
            if telegram_action_audit is not None:
                telegram_action_audit.record(
                    "access.invite_required",
                    result="rejected",
                    metadata={"reason_code": "invite_required"},
                )
            return None

        if telegram_action_audit is not None:
            telegram_action_audit.record(
                "access.invite_required",
                result="rejected",
                metadata={"reason_code": "invite_required"},
            )
        return None

    @staticmethod
    def _is_private_invited_start(message: Message) -> bool:
        return bool(
            message.chat.type == "private"
            and message.text
            and _INVITED_START_RE.fullmatch(message.text)
        )

    @staticmethod
    def _is_private_inbound(event: TelegramObject) -> bool:
        if isinstance(event, Message):
            return event.chat.type == "private"
        return False
