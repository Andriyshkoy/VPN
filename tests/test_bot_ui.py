from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, SuccessfulPayment, Update, User

import bot.handlers as handlers
from bot.handlers import (
    base,
    common,
    configs,
    navigation,
    payments,
    privacy,
    referrals,
)
from bot.instructions import GUIDE_MENU_TEXT, GUIDES
from bot.keyboards import (
    GUIDE_LABELS,
    MENU_BALANCE,
    MENU_CONFIGS,
    MENU_INSTRUCTIONS,
    MENU_REFERRALS,
    MENU_TOP_UP,
    guide_menu_keyboard,
    main_menu_keyboard,
)
from bot.states import CreateConfig


class DummyMessage:
    def __init__(self, text: str | None = None):
        self.text = text
        self.from_user = SimpleNamespace(id=101, username="alice")
        self.chat = SimpleNamespace(id=202)
        self.calls: list[tuple[str, dict]] = []
        self.edits: list[tuple[str, dict]] = []

    async def answer(self, text: str, **kwargs):
        self.calls.append((text, kwargs))

    async def edit_text(self, text: str, **kwargs):
        self.edits.append((text, kwargs))


class DummyCallback:
    def __init__(self, data: str):
        self.data = data
        self.from_user = SimpleNamespace(id=101, username="alice")
        self.message = DummyMessage()
        self.answers: list[tuple[tuple, dict]] = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


class DummyState:
    def __init__(self):
        self.cleared = False

    async def clear(self):
        self.cleared = True


def test_main_menu_keyboard_layout_and_persistence():
    keyboard = main_menu_keyboard()

    assert keyboard.resize_keyboard is True
    assert keyboard.is_persistent is True
    assert keyboard.one_time_keyboard is False
    assert [[button.text for button in row] for row in keyboard.keyboard] == [
        [MENU_BALANCE, MENU_CONFIGS],
        [MENU_TOP_UP, MENU_INSTRUCTIONS],
        [MENU_REFERRALS],
    ]


def test_navigation_router_has_priority_over_fsm_feature_router():
    assert handlers.router.sub_routers[0] is payments.payment_events_router
    assert handlers.router.sub_routers[1] is handlers.privacy_router
    assert handlers.router.sub_routers[2] is navigation.router
    assert handlers.router.sub_routers[3] is handlers.feature_router


@pytest.mark.asyncio
async def test_setup_commands_exposes_only_simple_navigation():
    captured = {}

    class Bot:
        async def set_my_commands(self, commands, **kwargs):
            captured["commands"] = commands

    await base.setup_bot_commands(Bot())

    assert [command.command for command in captured["commands"]] == [
        "start",
        "menu",
        "help",
    ]


@pytest.mark.asyncio
async def test_start_registers_payload_and_installs_menu(monkeypatch):
    captured = {}

    async def register(tg_id, username, ref_id=None):
        captured.update(tg_id=tg_id, username=username, ref_id=ref_id)

    monkeypatch.setattr(common, "get_or_create_user", register)
    message = DummyMessage("/start 42")

    await common.cmd_start(message, SimpleNamespace(args="42"))

    assert captured == {"tg_id": 101, "username": "alice", "ref_id": "42"}
    assert message.calls[-1][1]["reply_markup"].is_persistent is True
    assert "запоминания команд" in message.calls[-1][0]


@pytest.mark.asyncio
async def test_main_action_clears_fsm_before_shared_action(monkeypatch):
    state = DummyState()
    message = DummyMessage(MENU_BALANCE)
    observed = {}

    async def show_balance(target):
        observed["state_was_cleared"] = state.cleared
        observed["message"] = target

    monkeypatch.setattr(common, "cmd_balance", show_balance)

    await navigation.balance_navigation(message, state)

    assert observed == {"state_was_cleared": True, "message": message}


@pytest.mark.asyncio
async def test_config_fsm_gives_hints_instead_of_silently_ignoring_messages():
    choosing = DummyMessage("не кнопка")
    await configs.choose_server_message_hint(choosing)
    assert "кнопкой в сообщении выше" in choosing.calls[-1][0]
    assert choosing.calls[-1][1]["reply_markup"].is_persistent is True

    non_text_name = DummyMessage(None)
    await configs.config_name_message_hint(non_text_name)
    assert "отправить текстом" in non_text_name.calls[-1][0]
    assert "Отмена" in non_text_name.calls[-1][1]["reply_markup"].keyboard[0][0].text

    non_text_rename = DummyMessage(None)
    await configs.rename_message_hint(non_text_rename)
    assert "отправить текстом" in non_text_rename.calls[-1][0]


@pytest.mark.asyncio
async def test_group_ui_redirects_without_exposing_account_data():
    message = DummyMessage(MENU_BALANCE)
    message.chat.type = "group"

    await privacy.group_message(message)

    assert "только в личном чате" in message.calls[-1][0]
    callback = DummyCallback("cfg:1")
    callback.message.chat.type = "group"
    await privacy.group_callback(callback)
    assert callback.answers[-1][1] == {"show_alert": True}


@pytest.mark.asyncio
async def test_dispatcher_menu_button_wins_over_active_name_state(monkeypatch):
    storage = MemoryStorage()
    dispatcher = Dispatcher(storage=storage)
    dispatcher.include_router(handlers.router)
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    observed = []

    async def show_balance(message):
        observed.append(message.text)

    monkeypatch.setattr(common, "cmd_balance", show_balance)
    key = StorageKey(bot_id=bot.id, chat_id=202, user_id=101)
    await storage.set_state(key, CreateConfig.entering_name)
    update = Update(
        update_id=90001,
        message=Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=202, type="private"),
            from_user=User(id=101, is_bot=False, first_name="Alice"),
            text=MENU_BALANCE,
        ),
    )
    try:
        await dispatcher.feed_update(bot, update)
        assert observed == [MENU_BALANCE]
        assert await storage.get_state(key) is None
    finally:
        await dispatcher.storage.close()
        await bot.session.close()
        dispatcher.sub_routers.remove(handlers.router)
        handlers.router._parent_router = None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_id", "chat_type"),
    [(202, "private"), (-202, "group")],
)
async def test_successful_payment_wins_over_fsm_and_group_privacy(
    monkeypatch,
    chat_id,
    chat_type,
):
    storage = MemoryStorage()
    dispatcher = Dispatcher(storage=storage)
    dispatcher.include_router(handlers.router)

    class RecordingBot(Bot):
        def __init__(self):
            super().__init__("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
            self.requests = []

        async def __call__(self, method, request_timeout=None):
            self.requests.append(method)
            return None

    bot = RecordingBot()
    captured = []

    async def get_user(*args, **kwargs):
        return SimpleNamespace(id=7)

    async def record_payment(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(payments, "get_or_create_user", get_user)
    monkeypatch.setattr(
        payments.billing_service,
        "record_telegram_payment",
        record_payment,
    )
    key = StorageKey(bot_id=bot.id, chat_id=chat_id, user_id=101)
    await storage.set_state(key, CreateConfig.entering_name)
    update = Update(
        update_id=90002,
        message=Message(
            message_id=2,
            date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id, type=chat_type),
            from_user=User(id=101, is_bot=False, first_name="Alice"),
            successful_payment=SuccessfulPayment(
                currency="RUB",
                total_amount=10000,
                invoice_payload="topup:intent-id",
                telegram_payment_charge_id="telegram-charge",
                provider_payment_charge_id="provider-charge",
            ),
        ),
    )
    try:
        await dispatcher.feed_update(bot, update)
        assert len(captured) == 1
        assert captured[0]["intent_id"] == "intent-id"
        assert await storage.get_state(key) == CreateConfig.entering_name
        assert len(bot.requests) == 1
        assert bot.requests[0].chat_id == 101
    finally:
        await dispatcher.storage.close()
        await bot.session.close()
        dispatcher.sub_routers.remove(handlers.router)
        handlers.router._parent_router = None


def test_instructions_cover_all_platforms_and_fit_telegram_limit():
    expected = {"windows", "macos", "android", "ios", "linux", "tv", "troubleshooting"}
    assert set(GUIDES) == expected
    assert len(GUIDE_MENU_TEXT) < 4096
    assert all(0 < len(text) < 4096 for text in GUIDES.values())
    assert "openvpn.net" in GUIDES["windows"]
    assert "apps.apple.com" in GUIDES["ios"]
    assert "play.google.com" in GUIDES["android"]

    callbacks = {
        button.callback_data
        for row in guide_menu_keyboard().inline_keyboard
        for button in row
    }
    assert callbacks == {f"guide:{name}" for name in expected}
    assert set(GUIDE_LABELS) == expected


@pytest.mark.asyncio
async def test_guide_callback_edits_one_message_and_closes_spinner():
    callback = DummyCallback("guide:windows")

    await common.show_guide(callback)

    assert callback.message.calls == []
    assert callback.message.edits[-1][0] == GUIDES["windows"]
    assert callback.message.edits[-1][1]["disable_web_page_preview"] is True
    assert callback.answers == [((), {})]


@pytest.mark.asyncio
async def test_empty_config_list_still_offers_creation(monkeypatch):
    async def register(*args, **kwargs):
        return SimpleNamespace(id=7)

    async def empty(*args, **kwargs):
        return []

    monkeypatch.setattr(configs, "get_or_create_user", register)
    monkeypatch.setattr(configs.config_service, "list", empty)
    message = DummyMessage(MENU_CONFIGS)

    await configs.cmd_configs(message)

    text, kwargs = message.calls[-1]
    assert "пока нет конфигураций" in text
    first_button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert first_button.callback_data == "cfg:create"
    assert "Создать" in first_button.text


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["cfg:-1", "cfg:0", "cfg:1x", "cfg:"])
async def test_invalid_config_callback_ids_fail_closed(data):
    callback = DummyCallback(data)

    assert await configs._callback_id(callback, "cfg:") is None

    args, kwargs = callback.answers[-1]
    assert "устарела" in args[0]
    assert kwargs == {"show_alert": True}


@pytest.mark.asyncio
async def test_config_list_keeps_processing_and_terminal_failures_visible(monkeypatch):
    async def register(*args, **kwargs):
        return SimpleNamespace(id=7)

    async def list_configs(*args, **kwargs):
        return [
            SimpleNamespace(
                id=1,
                display_name="Телефон",
                suspended=False,
                desired_state="active",
                actual_state="provisioning",
                operation_status="failed",
                last_error="network details must stay private",
            ),
            SimpleNamespace(
                id=2,
                display_name="Ноутбук",
                suspended=False,
                desired_state="active",
                actual_state="provisioning",
                operation_status="exhausted",
                last_error="internal manager error",
            ),
            SimpleNamespace(
                id=3,
                display_name="Удалённый",
                suspended=False,
                desired_state="revoked",
                actual_state="revoked",
                operation_status="succeeded",
                last_error=None,
            ),
        ]

    monkeypatch.setattr(configs, "get_or_create_user", register)
    monkeypatch.setattr(configs.config_service, "list", list_configs)
    message = DummyMessage(MENU_CONFIGS)

    await configs.cmd_configs(message)

    text, kwargs = message.calls[-1]
    assert "обрабатывается" in text
    buttons = [
        button for row in kwargs["reply_markup"].inline_keyboard for button in row
    ]
    assert [button.callback_data for button in buttons] == [
        "cfg:create",
        "cfg:1",
        "cfg:2",
    ]
    assert buttons[1].text.startswith("⏳")
    assert buttons[2].text.startswith("⚠️")


@pytest.mark.asyncio
async def test_config_details_escape_user_controlled_html(monkeypatch):
    config = SimpleNamespace(
        id=5,
        owner_id=7,
        server_id=9,
        display_name="<Телефон & ноутбук>",
        suspended=False,
    )

    async def register(*args, **kwargs):
        return SimpleNamespace(id=7)

    async def get_config(*args, **kwargs):
        return config

    async def get_server(*args, **kwargs):
        return SimpleNamespace(name="<Основной>", location="RU & KZ")

    monkeypatch.setattr(configs, "get_or_create_user", register)
    monkeypatch.setattr(configs.config_service, "get", get_config)
    monkeypatch.setattr(configs.server_service, "get", get_server)
    callback = DummyCallback("cfg:5")

    await configs.show_config(callback)

    text = callback.message.edits[-1][0]
    assert "&lt;Телефон &amp; ноутбук&gt;" in text
    assert "&lt;Основной&gt;" in text
    assert "<Телефон" not in text


@pytest.mark.asyncio
async def test_terminal_config_error_is_friendly_and_hides_unsafe_actions(monkeypatch):
    config = SimpleNamespace(
        id=5,
        owner_id=7,
        server_id=9,
        display_name="Телефон",
        suspended=False,
        desired_state="active",
        actual_state="provisioning",
        operation_status="exhausted",
        last_error="API secret and internal exception",
    )

    async def register(*args, **kwargs):
        return SimpleNamespace(id=7)

    async def get_config(*args, **kwargs):
        return config

    async def get_server(*args, **kwargs):
        return SimpleNamespace(name="Основной", location="RU")

    monkeypatch.setattr(configs, "get_or_create_user", register)
    monkeypatch.setattr(configs.config_service, "get", get_config)
    monkeypatch.setattr(configs.server_service, "get", get_server)
    callback = DummyCallback("cfg:5")

    await configs.show_config(callback)

    text, kwargs = callback.message.edits[-1]
    assert "требуется проверка" in text
    assert "списывать её повторно не нужно" in text
    assert "API secret" not in text
    actions = [
        button.callback_data
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "dl:5" not in actions
    assert "sus:5" not in actions
    assert "uns:5" not in actions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "handler"),
    [
        ("sus:5", configs.suspend_config_cb),
        ("uns:5", configs.unsuspend_config_cb),
    ],
)
async def test_historical_manual_pause_callbacks_fail_closed(
    monkeypatch,
    data,
    handler,
):
    async def register(*args, **kwargs):
        return SimpleNamespace(id=7, balance=Decimal("100"))

    async def get_config(*args, **kwargs):
        return SimpleNamespace(id=5, owner_id=7)

    monkeypatch.setattr(configs, "get_or_create_user", register)
    monkeypatch.setattr(configs.config_service, "get", get_config)
    callback = DummyCallback(data)

    await handler(callback)

    args, kwargs = callback.answers[-1]
    assert "временно недоступн" in args[0]
    assert kwargs == {"show_alert": True}


@pytest.mark.asyncio
async def test_referrals_are_an_honest_placeholder_for_message_and_legacy_callback():
    message = DummyMessage(MENU_REFERRALS)
    await referrals.cmd_referrals(message)

    text = message.calls[-1][0]
    assert text == referrals.REFERRALS_PLACEHOLDER
    assert "пока не производятся" in text
    assert "https://t.me/" not in text

    callback = DummyCallback("refs:-1")
    await referrals.legacy_referrals_callback(callback)
    assert callback.message.edits[-1][0] == referrals.REFERRALS_PLACEHOLDER
    assert callback.answers == [((), {})]


@pytest.mark.asyncio
async def test_top_up_opens_amounts_without_dead_end_crypto_screen(monkeypatch):
    async def register(*args, **kwargs):
        return SimpleNamespace(id=7, balance=Decimal("0"))

    monkeypatch.setattr(payments, "get_or_create_user", register)
    message = DummyMessage(MENU_TOP_UP)

    await payments.cmd_topup(message)

    text, kwargs = message.calls[-1]
    assert "Выберите сумму" in text
    callbacks = [
        button.callback_data
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert callbacks == ["topup:100", "topup:200", "topup:300", "topup:500"]
