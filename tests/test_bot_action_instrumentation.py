from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram.types import Chat, Message, User

from bot.handlers import (
    balance_history,
    common,
    configs,
    fallback,
    navigation,
    payments,
    privacy,
    referrals,
)
from bot.middlewares.invite_access import InviteOnlyAccessMiddleware
from core.services import BalanceHistoryPage, TelegramActionAuditContext


class DummyMessage:
    def __init__(self, text: str | None = None, *, chat_type: str = "private"):
        self.text = text
        self.from_user = SimpleNamespace(id=101, username="alice")
        self.chat = SimpleNamespace(id=202, type=chat_type)
        self.bot = object()
        self.calls: list[tuple[str, dict]] = []
        self.edits: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs):
        self.calls.append((text, kwargs))

    async def edit_text(self, text: str, **kwargs):
        self.edits.append((text, kwargs))


class DummyCallback:
    def __init__(self, data: str, *, chat_type: str = "private"):
        self.data = data
        self.from_user = SimpleNamespace(id=101, username="alice")
        self.message = DummyMessage(chat_type=chat_type)
        self.bot = object()
        self.answers: list[tuple[tuple, dict]] = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


def audit(action: str = "update.received") -> TelegramActionAuditContext:
    return TelegramActionAuditContext(action=action, result="handled", metadata={})


@pytest.mark.asyncio
async def test_navigation_forwards_audit_context_to_nested_feature_handlers(
    monkeypatch,
):
    observed = {}

    async def configs_handler(message, telegram_action_audit=None):
        observed["configs"] = telegram_action_audit

    async def topup_handler(message, telegram_action_audit=None):
        observed["topup"] = telegram_action_audit

    async def create_handler(message, state, telegram_action_audit=None):
        observed["create"] = telegram_action_audit

    class State:
        async def clear(self):
            return None

    monkeypatch.setattr(configs, "cmd_configs", configs_handler)
    monkeypatch.setattr(payments, "cmd_topup", topup_handler)
    monkeypatch.setattr(configs, "cmd_create_config", create_handler)
    context = audit()
    state = State()

    await navigation.configs_navigation(DummyMessage(), state, context)
    await navigation.top_up_navigation(DummyMessage(), state, context)
    await navigation.create_config_navigation(DummyMessage(), state, context)

    assert observed == {
        "configs": context,
        "topup": context,
        "create": context,
    }


@pytest.mark.asyncio
async def test_start_records_completion_without_start_payload(monkeypatch):
    async def existing_user(*args, **kwargs):
        return SimpleNamespace(id=7)

    monkeypatch.setattr(common, "get_or_create_user", existing_user)
    context = audit("navigation.start")
    message = DummyMessage("/start ref_do-not-persist")

    await common.cmd_start(
        message,
        SimpleNamespace(args="ref_do-not-persist"),
        context,
    )

    assert context.action == "navigation.start"
    assert context.result == "completed"
    assert context.metadata == {}
    assert "do-not-persist" not in repr(context)


@pytest.mark.asyncio
async def test_invalid_guide_and_balance_cursor_record_only_safe_reasons():
    guide_context = audit("callback.received")
    guide_callback = DummyCallback("guide:raw-secret-guide")
    await common.show_guide(guide_callback, guide_context)

    assert guide_context.action == "navigation.guide_open"
    assert guide_context.result == "invalid"
    assert guide_context.metadata == {"reason_code": "unknown_guide"}
    assert "raw-secret-guide" not in repr(guide_context)

    history_context = audit("callback.received")
    history_callback = DummyCallback("balance_history:raw-secret-cursor")
    await balance_history.balance_history_callback(
        history_callback,
        history_context,
    )

    assert history_context.action == "finance.balance_history"
    assert history_context.result == "invalid"
    assert history_context.metadata == {"reason_code": "invalid_cursor"}
    assert "raw-secret-cursor" not in repr(history_context)


@pytest.mark.asyncio
async def test_verified_balance_history_keeps_only_direction(monkeypatch):
    page = BalanceHistoryPage(
        items=(),
        total=0,
        limit=8,
        offset=8,
        snapshot_id=42,
    )

    async def history(*args, **kwargs):
        return page

    monkeypatch.setattr(balance_history, "_history_for_telegram_user", history)
    context = audit("finance.balance_history")
    callback = DummyCallback("balance_history:debit:42:8")

    await balance_history.balance_history_callback(callback, context)

    assert context.action == "finance.balance_history"
    assert context.result == "completed"
    assert context.metadata == {"direction": "debit"}


@pytest.mark.asyncio
async def test_disabled_referrals_are_explicitly_unavailable(monkeypatch):
    async def screen(**kwargs):
        return "Referral screen", object()

    monkeypatch.setattr(referrals, "_referral_screen", screen)
    monkeypatch.setattr(referrals.settings, "referral_rewards_enabled", False)
    context = audit("referral.overview")

    await referrals.cmd_referrals(
        DummyMessage(),
        bot=object(),
        telegram_action_audit=context,
    )

    assert context.action == "referral.overview"
    assert context.result == "unavailable"
    assert context.metadata == {"reason_code": "referral_rewards_disabled"}


@pytest.mark.asyncio
async def test_privacy_and_fallback_record_rejection_without_message_text():
    privacy_context = audit("message.received")
    await privacy.group_message(
        DummyMessage("private balance", chat_type="group"),
        privacy_context,
    )
    assert privacy_context.action == "privacy.non_private_input"
    assert privacy_context.result == "rejected"
    assert privacy_context.metadata == {"reason_code": "private_chat_required"}
    assert "private balance" not in repr(privacy_context)

    fallback_context = audit("message.received")
    await fallback.unknown_text(
        DummyMessage("raw private message"),
        fallback_context,
    )
    assert fallback_context.action == "message.unrecognized"
    assert fallback_context.result == "invalid"
    assert fallback_context.metadata == {
        "content_type": "text",
        "reason_code": "unsupported_input",
    }
    assert "raw private message" not in repr(fallback_context)


@pytest.mark.asyncio
async def test_invite_middleware_records_denial_without_unknown_message_text():
    class MissingUserService:
        async def find_by_tg_id(self, tg_id: int, **kwargs):
            return None

    middleware = InviteOnlyAccessMiddleware(MissingUserService())
    context = audit("message.command_received")
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=202, type="private"),
        from_user=User(id=101, is_bot=False, first_name="Alice"),
        text="/start raw-secret-payload",
    )
    called = False

    async def handler(event, data):
        nonlocal called
        called = True

    result = await middleware(
        handler,
        message,
        {"telegram_action_audit": context},
    )

    assert result is None
    assert called is False
    assert context.action == "access.invite_required"
    assert context.result == "rejected"
    assert context.metadata == {"reason_code": "invite_required"}
    assert "raw-secret-payload" not in repr(context)


@pytest.mark.asyncio
async def test_cancel_records_only_allowlisted_flow_before_clearing():
    class State:
        cleared = False

        async def get_state(self):
            return "CreateConfig:entering_name"

        async def clear(self):
            self.cleared = True

    state = State()
    context = audit("navigation.cancel")

    await navigation.cancel_navigation(DummyMessage(), state, context)

    assert state.cleared is True
    assert context.action == "navigation.cancel"
    assert context.result == "completed"
    assert context.metadata == {"flow": "create_config"}
