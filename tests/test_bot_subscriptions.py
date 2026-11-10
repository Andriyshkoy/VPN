import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.handlers import base, common, subscriptions
from core.exceptions import APIConnectionError, InvalidOperationError
from core.services.subscription_types import (
    PLANS,
    SubscriptionAccessView,
    SubscriptionProfile,
    SubscriptionView,
)
from core.services.subscriptions import SubscriptionUnavailableError
from core.services.telegram_user_actions import (
    TelegramActionAuditContext,
    classify_telegram_action,
    sanitize_action_metadata,
)


class Message:
    def __init__(self):
        self.from_user = SimpleNamespace(id=99, username="wrong_bot_identity")
        self.calls = []
        self.edits = []
        self.documents = []

    async def answer(self, text, **kwargs):
        self.calls.append((text, kwargs))

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))

    async def answer_document(self, document, **kwargs):
        self.documents.append((document, kwargs))


class Callback:
    def __init__(self, data, *, callback_id="query-1"):
        self.data = data
        self.id = callback_id
        self.from_user = SimpleNamespace(id=101, username="alice")
        self.message = Message()
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


def view(**changes):
    data = SubscriptionView(
        id="sub-id",
        user_id=7,
        plan_code="plus",
        status="active",
        hourly_rate_rub=Decimal("0.14"),
        monthly_price_rub=Decimal("100"),
        monthly_estimate_rub=Decimal("100.80"),
        next_charge_at=None,
        billing_paused=True,
        unlimited_balance=False,
        services=("hysteria", "vless-reality"),
        accesses=(
            SubscriptionAccessView("hysteria", 1, "active", "remote-h", 1),
            SubscriptionAccessView("vless-reality", 2, "active", "remote-v", 1),
        ),
        version=3,
        max_sessions=None,
    )
    return replace(data, **changes)


def buttons(markup):
    return {button.callback_data for row in markup.inline_keyboard for button in row}


@pytest.fixture
def flow(monkeypatch):
    user = SimpleNamespace(id=7, balance=Decimal("9.80"), unlimited_balance=False)
    register = AsyncMock(return_value=user)
    service = SimpleNamespace(
        catalog=lambda: PLANS,
        get_current=AsyncMock(return_value=view()),
        select_plan=AsyncMock(return_value=view()),
        issue=AsyncMock(return_value=view()),
        regenerate=AsyncMock(return_value=view()),
        suspend=AsyncMock(return_value=view(status="suspended")),
        resume=AsyncMock(return_value=view()),
        export=AsyncMock(
            return_value=SubscriptionProfile(
                "vless-reality",
                "vless://key@example.com:443/?security=reality&type=tcp",
            )
        ),
    )
    monkeypatch.setattr(subscriptions, "get_or_create_user", register)
    monkeypatch.setattr(subscriptions, "subscription_service", service)
    monkeypatch.setattr(subscriptions.settings, "maintenance_mode", False)
    monkeypatch.setattr(subscriptions.settings, "provisioning_enabled", True)
    monkeypatch.setattr(subscriptions.settings, "billing_enabled", False)
    return SimpleNamespace(
        service=service,
        user=user,
        register=register,
        state=SimpleNamespace(clear=AsyncMock()),
    )


def assert_monthly_tariffs_only(text):
    for token in (
        "₽/час",
        "почас",
        "720",
        "0,07 ₽",
        "0,14 ₽",
        "50,40 ₽",
        "100,80 ₽",
    ):
        assert token not in text.lower()


def test_monthly_copy_preserves_wallet_without_exposing_internal_billing_schedule():
    text = subscriptions.render_subscription(view(), Decimal("9.80"))
    assert "100 ₽/месяц" in text
    assert "9,80 ₽" in text
    assert_monthly_tariffs_only(text)
    assert "Баланс не расходуется" in text
    assert "примерно на" not in text
    assert all(
        word not in text
        for word in ("Оплаченный период", "Продлить", "Срок закончился")
    )
    active = view(
        billing_paused=False,
        next_charge_at=datetime(2026, 11, 9, 11, tzinfo=timezone.utc),
    )
    text = subscriptions.render_subscription(active, Decimal("9.80"))
    assert_monthly_tariffs_only(text)
    assert "примерно на" not in text
    assert "09.11.2026 11:00 UTC" not in text
    assert "9,80 ₽" in text


@pytest.mark.parametrize("plan", PLANS, ids=lambda plan: plan.code)
@pytest.mark.parametrize("paused", [False, True])
def test_subscription_card_uses_monthly_dto_price_not_internal_rate_or_estimate(
    plan, paused
):
    subscription = view(
        plan_code=plan.code,
        monthly_price_rub=plan.monthly_price_rub,
        hourly_rate_rub=Decimal("987.65"),
        monthly_estimate_rub=Decimal("543.21"),
        services=plan.services,
        max_sessions=plan.max_sessions,
        billing_paused=paused,
    )
    text = subscriptions.render_subscription(subscription, Decimal("9.80"))
    assert f"{int(plan.monthly_price_rub)} ₽/месяц" in text
    assert "987,65" not in text and "543,21" not in text
    assert_monthly_tariffs_only(text)
    assert "9,80 ₽" in text


def test_unlimited_balance_hides_finite_wallet_without_fake_money():
    user = SimpleNamespace(unlimited_balance=True, balance=Decimal("123.45"))
    text = common.render_balance_summary(user)
    assert "∞" in text and "123,45" not in text
    text = subscriptions.render_subscription(
        view(unlimited_balance=True, billing_paused=False), user.balance
    )
    assert "∞" in text and "123,45" not in text and "примерно на" not in text
    assert user.balance == Decimal("123.45")


def test_pending_and_suspended_access_have_truthful_actions():
    _, markup = subscriptions.render_keys(view(status="pending", accesses=()))
    assert "sub:issue" in buttons(markup)
    # A partial/unfinished grant may already have a remote connection. Its
    # owner can pause the subscription before all credentials finish issuing.
    assert "sub:suspend-confirm" in buttons(
        subscriptions.subscription_keyboard(view(status="pending"))
    )
    text, markup = subscriptions.render_keys(view(status="suspended"))
    assert "приостановлен" in text
    assert "sub:issue" not in buttons(markup)
    assert not any(value.startswith("sub:profile:") for value in buttons(markup))
    assert "sub:resume-confirm" in buttons(
        subscriptions.subscription_keyboard(view(status="expired"))
    )


@pytest.mark.asyncio
async def test_choose_plan_is_read_only_until_confirmation(flow):
    callback = Callback("sub:choose:basic")
    await subscriptions.subscription_callback(callback, flow.state)
    flow.service.select_plan.assert_not_awaited()
    text, kwargs = callback.message.edits[-1]
    assert "BASIC — 50 ₽/месяц" in text
    assert "Instagram" in text and "один клиент одновременно" in text
    assert_monthly_tariffs_only(text)
    assert "не спишет деньги" in text
    assert "sub:apply:basic:3" in buttons(kwargs["reply_markup"])
    flow.register.assert_awaited_once_with(101, "alice")
    flow.state.clear.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        "plans",
        "choose:basic",
        "choose:plus",
        "view",
        "keys",
        "suspend-confirm",
        "resume-confirm",
    ],
)
async def test_subscription_callback_screens_do_not_expose_hourly_tariffs(flow, action):
    callback = Callback(f"sub:{action}")
    await subscriptions.subscription_callback(callback, flow.state)
    text, kwargs = callback.message.edits[-1]
    assert_monthly_tariffs_only(text)
    for row in kwargs["reply_markup"].inline_keyboard:
        for button in row:
            assert_monthly_tariffs_only(button.text)
    if action == "plans":
        labels = [
            button.text
            for row in kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        assert "BASIC · 50 ₽/месяц" in labels
        assert "PLUS · 100 ₽/месяц" in labels
    if action in {"plans", "choose:plus", "view"}:
        assert "нескольких устройств" in text
        assert "Hysteria 2" in text and "VLESS + REALITY" in text
        assert "выберите другой вручную" in text


@pytest.mark.asyncio
async def test_plan_confirmation_is_versioned_and_duplicate_does_not_reapply(flow):
    callback = Callback("sub:apply:basic:3")
    selected = view(plan_code="basic", version=4)
    flow.service.select_plan.return_value = selected
    await subscriptions.subscription_callback(callback, flow.state)
    args = flow.service.select_plan.await_args
    assert args.args == (7, "basic")
    assert args.kwargs["expected_version"] == 3
    flow.service.get_current.return_value = selected
    await subscriptions.subscription_callback(
        Callback(callback.data, callback_id="second-click"), flow.state
    )
    assert flow.service.select_plan.await_count == 1
    flow.service.get_current.return_value = view(version=9)
    await subscriptions.subscription_callback(Callback(callback.data), flow.state)
    assert flow.service.select_plan.await_count == 1


@pytest.mark.asyncio
async def test_issue_is_free_owner_scoped_and_has_stable_replay_key(flow, monkeypatch):
    forbidden_billing = AsyncMock(
        side_effect=AssertionError("key issue must not debit")
    )
    monkeypatch.setattr(base.billing_service, "create_paid_config", forbidden_billing)
    callback = Callback("sub:issue")
    context = TelegramActionAuditContext("callback.received", "handled", {})
    await subscriptions.subscription_callback(callback, flow.state, context)
    await subscriptions.subscription_callback(callback, flow.state, context)
    first, second = flow.service.issue.await_args_list
    assert first == second
    assert first.args == (7,)
    assert first.kwargs["request_key"]
    forbidden_billing.assert_not_awaited()
    assert context.action == "vpn.key_issue"
    assert flow.user.balance == Decimal("9.80")


@pytest.mark.asyncio
async def test_regeneration_requires_confirmation_and_same_button_rotates_once_key(
    flow,
):
    request = Callback("sub:rotate-confirm:hysteria")
    await subscriptions.subscription_callback(request, flow.state)
    flow.service.regenerate.assert_not_awaited()
    text, kwargs = request.message.edits[-1]
    assert "на всех устройствах" in text and "Другой сервис не изменится" in text
    confirmed = next(
        value
        for value in buttons(kwargs["reply_markup"])
        if value.startswith("sub:rotate:")
    )
    await subscriptions.subscription_callback(Callback(confirmed), flow.state)
    await subscriptions.subscription_callback(
        Callback(confirmed, callback_id="another-click"), flow.state
    )
    first, second = flow.service.regenerate.await_args_list
    assert first == second
    assert first.args == (7, "hysteria")
    assert len(confirmed.encode()) <= 64


@pytest.mark.asyncio
async def test_secret_export_has_no_callback_keyboard_and_never_enters_audit(flow):
    callback = Callback("sub:profile:vless-reality")
    context = TelegramActionAuditContext("callback.received", "handled", {})
    await subscriptions.subscription_callback(callback, flow.state, context)
    flow.service.export.assert_awaited_once_with(7, "vless-reality")
    text, kwargs = callback.message.calls[0]
    assert "vless://key@" in text and "&amp;type=tcp" in text
    assert "reply_markup" not in kwargs
    assert "protect_content" not in kwargs  # users must be able to copy the URI
    text, kwargs = callback.message.calls[1]
    assert "vless://" not in text
    assert kwargs["reply_markup"] is not None
    assert context.metadata == {"service_id": "vless-reality"}
    assert "key@" not in repr(context)
    assert callback.message.edits == []


@pytest.mark.asyncio
async def test_long_uri_is_delivered_intact_as_copyable_file(flow):
    uri = "vless://key@example.com:443/?padding=" + "a" * 4000
    flow.service.export.return_value = SubscriptionProfile("vless-reality", uri)
    callback = Callback("sub:profile:vless-reality")
    await subscriptions.subscription_callback(callback, flow.state)
    document, kwargs = callback.message.documents[0]
    assert document.data == uri.encode()
    assert document.filename == "hiddify-vless-reality.txt"
    assert "reply_markup" not in kwargs
    assert uri not in str(callback.message.calls)


@pytest.mark.asyncio
async def test_hysteria_json_is_a_secret_file_and_copyable_content_without_callbacks(
    flow,
):
    content = json.dumps(
        {
            "outbounds": [
                {
                    "type": "hysteria2",
                    "password": "user:private<&>",
                    "tls": {
                        "enabled": True,
                        "insecure": False,
                        "certificate_sha256": "a" * 64,
                    },
                }
            ]
        }
    )
    flow.service.export.return_value = SubscriptionProfile(
        "hysteria", uri=None, format="singbox-json", content=content
    )
    callback = Callback("sub:profile:hysteria")
    context = TelegramActionAuditContext("callback.received", "handled", {})
    await subscriptions.subscription_callback(callback, flow.state, context)
    document, kwargs = callback.message.documents[0]
    assert document.filename == "hiddify-hysteria.json"
    assert document.data == content.encode()
    assert "reply_markup" not in kwargs and "protect_content" not in kwargs
    text, kwargs = callback.message.calls[0]
    assert "JSON для Hiddify" in text and "private&lt;&amp;&gt;" in text
    assert "reply_markup" not in kwargs
    instruction, kwargs = callback.message.calls[1]
    assert "фигурные скобки" in instruction and "«Файл»" in instruction
    assert content not in instruction and kwargs["reply_markup"] is not None
    assert context.metadata == {"service_id": "hysteria"}
    assert "private" not in repr(context)


@pytest.mark.asyncio
async def test_long_json_remains_intact_in_file_without_oversized_message(flow):
    content = json.dumps({"outbounds": [], "fixture": "<&>" * 2000})
    flow.service.export.return_value = SubscriptionProfile(
        "hysteria", uri=None, format="singbox-json", content=content
    )
    callback = Callback("sub:profile:hysteria")
    await subscriptions.subscription_callback(callback, flow.state)
    assert callback.message.documents[0][0].data == content.encode()
    assert callback.message.documents[0][0].filename.endswith(".json")
    assert len(callback.message.calls) == 1
    assert content not in str(callback.message.calls)


@pytest.mark.asyncio
async def test_hysteria_uri_downgrade_is_not_exported_to_hiddify(flow):
    flow.service.export.return_value = SubscriptionProfile(
        "hysteria",
        "hysteria2://private@example.com:1935/?insecure=1&pinSHA256=" + "a" * 64,
    )
    callback = Callback("sub:profile:hysteria")
    await subscriptions.subscription_callback(callback, flow.state)
    assert callback.message.calls == [] and callback.message.documents == []
    assert callback.answers[-1][1] == {"show_alert": True}
    assert "private" not in str(callback.answers)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["no_active_node", "session_limit_unavailable"])
async def test_unavailable_server_capability_has_safe_user_message(flow, reason):
    flow.service.issue.side_effect = SubscriptionUnavailableError(reason)
    callback = Callback("sub:issue")
    context = TelegramActionAuditContext("callback.received", "handled", {})
    await subscriptions.subscription_callback(callback, flow.state, context)
    assert callback.answers[-1][1] == {"show_alert": True}
    assert "Сервер" in callback.answers[-1][0][0]
    assert context.metadata == {"reason_code": reason}
    assert context.result == "unavailable"


@pytest.mark.asyncio
async def test_backend_rejects_forbidden_profile_without_leaking_exception(flow):
    flow.service.export.side_effect = InvalidOperationError("private internal secret")
    callback = Callback("sub:profile:vless-reality")
    await subscriptions.subscription_callback(callback, flow.state)
    assert callback.message.calls == []
    assert "private internal" not in str(callback.answers)
    assert callback.answers[-1][1] == {"show_alert": True}


@pytest.mark.asyncio
async def test_transient_issue_preserves_durable_retry_semantics(flow):
    flow.service.issue.side_effect = APIConnectionError("unknown server result")
    callback = Callback("sub:issue")
    with pytest.raises(APIConnectionError):
        await subscriptions.subscription_callback(callback, flow.state)
    assert callback.message.edits == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        "sub:profile:ovpn",
        "sub:issue:42",
        "sub:apply:basic",
        "sub:apply:basic:03",
        "sub:apply:enterprise:3",
        "sub:rotate:hysteria:secret",
        "sub:resume:-1",
    ],
)
async def test_malformed_callbacks_do_not_read_or_mutate_another_account(flow, data):
    callback = Callback(data)
    await subscriptions.subscription_callback(callback, flow.state)
    flow.register.assert_not_awaited()
    flow.service.get_current.assert_not_awaited()
    assert callback.answers[-1][1] == {"show_alert": True}


@pytest.mark.asyncio
async def test_provisioning_gate_blocks_issue_and_rotate_without_charging(
    flow, monkeypatch
):
    monkeypatch.setattr(subscriptions.settings, "provisioning_enabled", False)
    for data in ("sub:issue", "sub:rotate:hysteria:" + "a" * 20):
        await subscriptions.subscription_callback(Callback(data), flow.state)
    flow.service.issue.assert_not_awaited()
    flow.service.regenerate.assert_not_awaited()


@pytest.mark.asyncio
async def test_suspension_requires_explicit_confirmation(flow):
    request = Callback("sub:suspend-confirm")
    await subscriptions.subscription_callback(request, flow.state)
    flow.service.suspend.assert_not_awaited()
    data = next(
        value
        for value in buttons(request.message.edits[-1][1]["reply_markup"])
        if value.startswith("sub:suspend:")
    )
    confirmed = Callback(data)
    await subscriptions.subscription_callback(confirmed, flow.state)
    assert flow.service.suspend.await_args.args == (7,)
    assert "sub:resume-confirm" in buttons(
        confirmed.message.edits[-1][1]["reply_markup"]
    )


def test_subscription_audit_only_persists_strict_product_enums():
    assert sanitize_action_metadata(
        {"service_id": "hysteria", "plan_code": "plus", "uri": "secret"}
    ) == {"plan_code": "plus", "service_id": "hysteria"}
    assert (
        sanitize_action_metadata(
            {"service_id": "hysteria2://secret", "plan_code": "raw-private"}
        )
        == {}
    )
    payload = {
        "callback_query": {
            "from": {"id": 101},
            "data": "sub:rotate:hysteria:" + "a" * 20,
            "message": {"chat": {"type": "private"}},
        }
    }
    classified = classify_telegram_action(payload)
    assert classified.action == "vpn.key_rotate"
    assert classified.metadata == {"service_id": "hysteria"}
    payload["callback_query"]["data"] = "sub:apply:basic:3"
    classified = classify_telegram_action(payload)
    assert classified.metadata == {"plan_code": "basic"}
    payload["callback_query"]["data"] = "sub:profile:vless://raw-secret"
    classified = classify_telegram_action(payload)
    assert classified.action == "callback.received" and classified.metadata == {}
