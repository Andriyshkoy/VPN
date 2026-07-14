from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class TelegramActionClassification:
    telegram_user_id: int | None
    action: str
    result: str
    metadata: dict[str, object]


_ALLOWED_RESULTS = frozenset(
    {"handled", "completed", "rejected", "ignored", "invalid", "unavailable", "failed"}
)
_SAFE_METADATA_TYPES: dict[str, type] = {
    "content_type": str,
    "guide": str,
    "direction": str,
    "provider": str,
    "amount_rub": int,
    "config_id": int,
    "server_id": int,
    "attempts": int,
    "error_type": str,
    "reason_code": str,
    "flow": str,
}
_SAFE_FLOW_VALUES = frozenset({"create_config", "rename_config", "none"})


def sanitize_action_metadata(value: object) -> dict[str, object]:
    """Allow only scalar operational fields; discard all unknown input."""

    if not isinstance(value, dict):
        return {}
    result: dict[str, object] = {}
    for key, expected_type in _SAFE_METADATA_TYPES.items():
        candidate = value.get(key)
        if isinstance(candidate, bool) or not isinstance(candidate, expected_type):
            continue
        if isinstance(candidate, str):
            candidate = candidate[:64]
            if key == "flow" and candidate not in _SAFE_FLOW_VALUES:
                continue
        elif candidate < 0:
            continue
        result[key] = candidate
    return result


@dataclass(slots=True)
class TelegramActionAuditContext:
    """Mutable, request-local outcome staged by verified bot handlers."""

    action: str
    result: str
    metadata: dict[str, object]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TelegramActionAuditContext":
        classified = classify_telegram_action(payload)
        return cls(classified.action, classified.result, dict(classified.metadata))

    def record(
        self,
        action: str,
        *,
        result: str = "completed",
        metadata: dict[str, object] | None = None,
    ) -> None:
        if not action or len(action) > 96:
            raise ValueError("invalid Telegram audit action")
        if result not in _ALLOWED_RESULTS:
            raise ValueError("invalid Telegram audit result")
        self.action = action
        self.result = result
        self.metadata = sanitize_action_metadata(metadata or {})


_COMMAND_ACTIONS = {
    "start": "navigation.start",
    "menu": "navigation.menu",
    "help": "navigation.help",
    "balance": "finance.balance_view",
    "history": "finance.balance_history",
    "configs": "vpn.config_list",
    "topup": "finance.topup_open",
    "how_to_use": "navigation.instructions_open",
    "referrals": "referral.overview",
    "create_config": "vpn.config_create_start",
    "cancel": "navigation.cancel",
}

# These labels are inspected in memory only and are never copied to metadata.
_MENU_ACTIONS = {
    "💰 Баланс": "finance.balance_view",
    "🗂 Мои конфиги": "vpn.config_list",
    "💳 Пополнить": "finance.topup_open",
    "📚 Инструкции": "navigation.instructions_open",
    "🎁 Реферальная программа": "referral.overview",
    "❌ Отмена": "navigation.cancel",
}

_GUIDES = frozenset(
    {"menu", "windows", "macos", "android", "ios", "linux", "tv", "troubleshooting"}
)
_PAYMENT_PROVIDERS = frozenset({"telegram", "crypto"})
_TOP_UP_AMOUNTS = frozenset({100, 200, 300, 500})
_CONTENT_TYPES = (
    "text",
    "photo",
    "document",
    "audio",
    "video",
    "voice",
    "video_note",
    "animation",
    "sticker",
    "contact",
    "location",
    "venue",
    "poll",
    "dice",
)
_KNOWN_UPDATE_TYPES = frozenset(
    {
        "message",
        "edited_message",
        "channel_post",
        "edited_channel_post",
        "inline_query",
        "chosen_inline_result",
        "callback_query",
        "shipping_query",
        "pre_checkout_query",
        "poll",
        "poll_answer",
        "my_chat_member",
        "chat_member",
        "chat_join_request",
        "message_reaction",
        "message_reaction_count",
        "chat_boost",
        "removed_chat_boost",
    }
)
_SAFE_ERROR_TYPES = {
    "TimeoutError": "timeout",
    "ConnectionError": "network",
    "TelegramNetworkError": "network",
    "TelegramRetryAfter": "telegram_rate_limit",
    "TelegramAPIError": "telegram_api",
    "SQLAlchemyError": "database",
    "OperationalError": "database",
    "IntegrityError": "database",
    "RuntimeError": "runtime",
    "ValueError": "validation",
}


def _safe_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _telegram_user_id(payload: dict[str, Any]) -> int | None:
    for update_type in (
        "callback_query",
        "message",
        "edited_message",
        "pre_checkout_query",
        "shipping_query",
        "inline_query",
        "chosen_inline_result",
        "poll_answer",
        "chat_member",
        "my_chat_member",
        "chat_join_request",
    ):
        event = payload.get(update_type)
        if not isinstance(event, dict):
            continue
        sender = event.get("from") or event.get("user")
        if isinstance(sender, dict):
            user_id = _safe_int(sender.get("id"))
            if user_id is not None:
                return user_id
    return None


def _message_action(message: dict[str, Any]) -> tuple[str, dict[str, object]]:
    if isinstance(message.get("successful_payment"), dict):
        return "finance.payment_successful", {}

    text = message.get("text")
    if isinstance(text, str):
        if text.startswith("/"):
            command = text.split(None, 1)[0][1:].split("@", 1)[0].casefold()
            action = _COMMAND_ACTIONS.get(command)
            if action is not None:
                return action, {}
            return "message.command_received", {}
        action = _MENU_ACTIONS.get(text)
        if action is not None:
            return action, {}

    content_type = next((key for key in _CONTENT_TYPES if key in message), "other")
    return "message.received", {"content_type": content_type}


def _callback_action(callback: dict[str, Any]) -> tuple[str, dict[str, object]]:
    data = callback.get("data")
    if not isinstance(data, str):
        return "callback.received", {}

    if data == "balance_summary":
        return "finance.balance_view", {}
    if data.startswith("balance_history:"):
        parts = data.split(":")
        metadata: dict[str, object] = {}
        if len(parts) >= 2 and parts[1] in {"credit", "debit"}:
            metadata["direction"] = parts[1]
        return "finance.balance_history", metadata
    if data.startswith("guide:"):
        guide = data.partition(":")[2]
        if guide in _GUIDES:
            return "navigation.guide_open", {"guide": guide}
        return "callback.received", {}
    if data.startswith("pay:"):
        provider = data.partition(":")[2]
        if provider in _PAYMENT_PROVIDERS:
            return "finance.payment_provider_select", {"provider": provider}
        return "callback.received", {}
    if data.startswith("topup:"):
        raw_amount = data.partition(":")[2]
        if raw_amount.isascii() and raw_amount.isdecimal():
            amount = int(raw_amount)
            if amount in _TOP_UP_AMOUNTS:
                return "finance.payment_amount_select", {"amount_rub": amount}
        return "callback.received", {}
    if data.startswith("refs:"):
        return "referral.overview", {}

    exact_config_actions = {
        "cfg:create": "vpn.config_create_start",
        "cfg:list": "vpn.config_list",
    }
    if data in exact_config_actions:
        return exact_config_actions[data], {}
    config_patterns = (
        (r"cfg:(\d+)", "vpn.config_view"),
        (r"server:(\d+)", "vpn.config_server_select"),
        (r"sus:(\d+)", "vpn.config_suspend"),
        (r"uns:(\d+)", "vpn.config_resume"),
        (r"del:(\d+)", "vpn.config_delete_request"),
        (r"del_ok:(\d+)", "vpn.config_delete_confirm"),
        (r"dl:(\d+)", "vpn.config_download"),
        (r"rn:(\d+)", "vpn.config_rename_start"),
    )
    for pattern, action in config_patterns:
        match = re.fullmatch(pattern, data)
        if match:
            # The numeric target is untrusted until a handler verifies
            # ownership. A handler may add it through TelegramActionAuditContext.
            return action, {}
    return "callback.received", {}


def classify_telegram_action(payload: dict[str, Any]) -> TelegramActionClassification:
    """Reduce an untrusted Telegram update to a non-sensitive taxonomy."""

    telegram_user_id = _telegram_user_id(payload)
    message = payload.get("message")
    if isinstance(message, dict):
        chat = message.get("chat")
        if isinstance(chat, dict) and chat.get("type") != "private":
            action, metadata, result = "privacy.non_private_input", {}, "ignored"
        else:
            action, metadata = _message_action(message)
            result = "handled"
    elif isinstance(payload.get("callback_query"), dict):
        callback = payload["callback_query"]
        callback_message = callback.get("message")
        chat = (
            callback_message.get("chat") if isinstance(callback_message, dict) else None
        )
        if isinstance(chat, dict) and chat.get("type") != "private":
            action, metadata, result = "privacy.non_private_input", {}, "ignored"
        else:
            action, metadata = _callback_action(callback)
            result = "handled"
    elif isinstance(payload.get("pre_checkout_query"), dict):
        action, metadata, result = "finance.payment_pre_checkout", {}, "handled"
    else:
        update_type = next(
            (
                key
                for key in payload
                if key != "update_id" and key in _KNOWN_UPDATE_TYPES
            ),
            None,
        )
        action = f"{update_type}.received" if update_type else "update.received"
        metadata = {}
        result = "handled"
    return TelegramActionClassification(telegram_user_id, action, result, metadata)


def safe_error_type(error: Exception | str) -> str:
    """Map exception classes to a small allowlist; never persist error text."""

    if isinstance(error, str):
        return "unknown"
    for cls in type(error).__mro__:
        mapped = _SAFE_ERROR_TYPES.get(cls.__name__)
        if mapped is not None:
            return mapped
    return "unknown"


class TelegramUserActionService:
    """Append classified events inside the Telegram inbox transaction."""

    @staticmethod
    async def append_in_transaction(
        repos,
        *,
        source_update_id: int,
        payload: dict,
        result: str,
        occurred_at: datetime,
        failure_metadata: dict[str, object] | None = None,
        audit_context: TelegramActionAuditContext | None = None,
    ) -> bool:
        classification = classify_telegram_action(payload)
        if classification.telegram_user_id is None:
            return False
        user = await repos.users.get_by_tg_id(classification.telegram_user_id)
        if user is None:
            return False
        event_action = audit_context.action if audit_context else classification.action
        event_result = (
            "failed"
            if result == "failed"
            else (audit_context.result if audit_context else classification.result)
        )
        if event_result == "failed":
            metadata_input = dict(audit_context.metadata) if audit_context else {}
            metadata_input.update(failure_metadata or {})
        else:
            metadata_input = (
                audit_context.metadata
                if audit_context is not None
                else classification.metadata
            )
        metadata = sanitize_action_metadata(metadata_input)
        _, created = await repos.telegram_user_actions.append_once(
            user_id=user.id,
            source_update_id=source_update_id,
            action=event_action,
            result=event_result,
            metadata=metadata,
            occurred_at=occurred_at,
        )
        return created
