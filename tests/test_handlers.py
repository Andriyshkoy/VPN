import os
import tempfile
import types

import pytest

import bot.handlers as handlers
import bot.handlers.base as handlers_base
from core.services.telegram_user_actions import TelegramActionAuditContext


class DummyMessage:
    def __init__(self, text):
        self.text = text
        self.from_user = types.SimpleNamespace(id=1, username="user")
        self.chat = types.SimpleNamespace(id=123)
        self.answers = []
        self.answer_kwargs = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)
        self.answer_kwargs.append(kwargs)


class DummyState:
    def __init__(self):
        self.data = {"server_id": 1}
        self.cleared = False

    async def get_data(self):
        return self.data

    async def clear(self):
        self.cleared = True


class DummyStateRename:
    def __init__(self):
        self.data = {}
        self.updated = None
        self.state = None
        self.cleared = False

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def set_state(self, state):
        self.state = state

    async def get_data(self):
        return self.data

    async def clear(self):
        self.cleared = True


class DummyBot:
    def __init__(self):
        self.sent = None

    async def send_document(self, chat_id, file):
        self.chat_id = chat_id
        self.sent = file


class DummyFSInputFile:
    def __init__(self, path, filename=None):
        self.path = path
        self.filename = filename


@pytest.mark.asyncio
async def test_retired_config_name_flow_never_creates_or_charges_on_replay(monkeypatch):
    """An old FSM persisted before upgrade cannot create a legacy paid key."""
    from bot.handlers import subscriptions

    msg = DummyMessage("../../etc/passwd")
    state = DummyState()
    bot = DummyBot()
    observed = []

    async def show_subscription(message, **kwargs):
        assert state.cleared is True
        observed.append((message, kwargs))

    async def forbidden(*args, **kwargs):
        pytest.fail("Retired config creation reached a legacy payment or key path")

    monkeypatch.setattr(subscriptions, "cmd_subscription", show_subscription)
    monkeypatch.setattr(handlers.billing_service, "create_paid_config", forbidden)
    monkeypatch.setattr(handlers.config_service, "download_config", forbidden)
    audit = TelegramActionAuditContext("message.received", "handled", {})
    update = types.SimpleNamespace(update_id=4242)

    await handlers.got_name(msg, state, bot, update, telegram_action_audit=audit)
    await handlers.got_name(msg, state, bot, update, telegram_action_audit=audit)

    assert state.cleared is True
    assert bot.sent is None
    assert len(observed) == 2
    assert all(message is msg for message, _ in observed)
    assert all(kwargs["tg_id"] == msg.from_user.id for _, kwargs in observed)
    assert all(kwargs["username"] == msg.from_user.username for _, kwargs in observed)
    assert all(kwargs["telegram_action_audit"] is audit for _, kwargs in observed)
    assert "../../etc/passwd" not in repr(audit.metadata)


class DummyMessageReply:
    def __init__(self):
        self.chat = types.SimpleNamespace(id=123)
        self.answers = []

    async def answer(self, text, reply_markup=None, **_):
        self.answers.append((text, reply_markup))

    async def edit_text(self, text, reply_markup=None, **_):
        self.answers.append((text, reply_markup))


class DummyCallback:
    def __init__(self, data="cfg:1"):
        self.data = data
        self.from_user = types.SimpleNamespace(id=1, username="user")
        self.message = DummyMessageReply()
        self.answered = False

    async def answer(self, text=None, show_alert=False):
        self.answered = True


@pytest.mark.asyncio
async def test_show_config_contains_download(monkeypatch):
    cb = DummyCallback()

    cfg = types.SimpleNamespace(
        id=1,
        owner_id=1,
        server_id=2,
        display_name="name",
        suspended=False,
    )
    server = types.SimpleNamespace(name="srv", location="loc")

    async def fake_get_user(*a, **kw):
        return types.SimpleNamespace(id=1)

    monkeypatch.setattr(handlers, "get_or_create_user", fake_get_user)
    monkeypatch.setattr(handlers_base, "get_or_create_user", fake_get_user)
    monkeypatch.setattr(handlers.configs, "get_or_create_user", fake_get_user)

    async def fake_get(*a, **kw):
        return cfg

    async def fake_server(*a, **kw):
        return server

    monkeypatch.setattr(handlers.config_service, "get", fake_get)
    monkeypatch.setattr(handlers.server_service, "get", fake_server)

    await handlers.show_config(cb)

    markup = cb.message.answers[0][1]
    button_texts = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Скачать" in text for text in button_texts)
    assert any("Переименовать" in text for text in button_texts)
    assert not any("Приостановить" in text for text in button_texts)
    assert not any("Возобновить" in text for text in button_texts)


@pytest.mark.asyncio
async def test_foreign_config_callback_audit_does_not_persist_requested_id(monkeypatch):
    cb = DummyCallback("del_ok:987654")
    audit = TelegramActionAuditContext(
        "vpn.config_delete_confirm",
        "handled",
        {},
    )

    async def fake_get_user(*_args, **_kwargs):
        return types.SimpleNamespace(id=1)

    async def fake_get_config(*_args, **_kwargs):
        return types.SimpleNamespace(id=987654, owner_id=2)

    monkeypatch.setattr(handlers.configs, "get_or_create_user", fake_get_user)
    monkeypatch.setattr(handlers.config_service, "get", fake_get_config)

    await handlers.configs.confirm_delete_config_cb(
        cb,
        telegram_action_audit=audit,
    )

    assert audit.action == "vpn.config_delete_confirm"
    assert audit.result == "rejected"
    assert audit.metadata == {"reason_code": "config_not_found"}
    assert "987654" not in repr(audit.metadata)


@pytest.mark.asyncio
async def test_retired_server_callback_uses_actor_identity_without_legacy_lookup(
    monkeypatch,
):
    from bot.handlers import subscriptions

    cb = DummyCallback("server:987654")
    cb.message.from_user = types.SimpleNamespace(id=999, username="bot")
    state = DummyStateRename()
    audit = TelegramActionAuditContext("callback.received", "handled", {})
    observed = []

    async def show_subscription(message, **kwargs):
        assert state.cleared is True
        observed.append((message, kwargs))

    async def forbidden(*args, **kwargs):
        pytest.fail("Retired server selection attempted a legacy server lookup")

    monkeypatch.setattr(subscriptions, "cmd_subscription", show_subscription)
    monkeypatch.setattr(handlers.server_service, "get", forbidden)

    await handlers.configs.choose_server(cb, state, telegram_action_audit=audit)

    assert cb.answered is True
    assert observed[0][0] is cb.message
    assert observed[0][1]["tg_id"] == cb.from_user.id
    assert observed[0][1]["username"] == cb.from_user.username
    assert observed[0][1]["telegram_action_audit"] is audit
    assert "987654" not in repr(audit.metadata)


class DummyCallbackDownload:
    def __init__(self):
        self.data = "dl:5"
        self.from_user = types.SimpleNamespace(id=1, username="user")
        self.message = types.SimpleNamespace(chat=types.SimpleNamespace(id=123))
        self.answered = False

    async def answer(self, text=None, show_alert=False):
        self.answered = True


@pytest.mark.asyncio
async def test_download_tempfile_used(monkeypatch):
    cb = DummyCallbackDownload()
    bot = DummyBot()

    cfg = types.SimpleNamespace(id=5, owner_id=1, display_name="disp")

    async def fake_get_user(*a, **kw):
        return types.SimpleNamespace(id=1)

    monkeypatch.setattr(handlers, "get_or_create_user", fake_get_user)
    monkeypatch.setattr(handlers_base, "get_or_create_user", fake_get_user)

    async def fake_get_config(*a, **kw):
        return cfg

    monkeypatch.setattr(handlers.configs, "get_or_create_user", fake_get_user)

    async def fake_download(*a, **kw):
        return b"data"

    monkeypatch.setattr(handlers.config_service, "get", fake_get_config)
    monkeypatch.setattr(handlers.config_service, "download_config", fake_download)
    monkeypatch.setattr(handlers, "FSInputFile", DummyFSInputFile)

    await handlers.download_config_cb(cb, bot)

    sent_path = bot.sent.path
    tmp_dir = tempfile.gettempdir()
    assert os.path.commonpath([sent_path, tmp_dir]) == tmp_dir
    assert not os.path.exists(sent_path)
    assert bot.chat_id == cb.from_user.id


@pytest.mark.asyncio
async def test_rename_callback_sets_state(monkeypatch):
    cb = DummyCallback("rn:3")
    state = DummyStateRename()

    async def fake_get_user(*a, **kw):
        return types.SimpleNamespace(id=1)

    async def fake_get_config(*a, **kw):
        return types.SimpleNamespace(id=3, owner_id=1)

    monkeypatch.setattr(handlers.configs, "get_or_create_user", fake_get_user)
    monkeypatch.setattr(handlers.config_service, "get", fake_get_config)

    await handlers.rename_config_cb(cb, state)

    assert state.data["config_id"] == 3
    assert state.state == handlers.RenameConfig.entering_name
    assert "Введите новое название" in cb.message.answers[-1][0]


@pytest.mark.asyncio
async def test_got_new_name(monkeypatch):
    msg = DummyMessage("new")
    state = DummyStateRename()
    state.data = {"config_id": 5}

    cfg = types.SimpleNamespace(id=5, owner_id=1)
    called = {}

    async def fake_get_user(*a, **kw):
        return types.SimpleNamespace(id=1)

    async def fake_get(*a, **kw):
        return cfg

    async def fake_rename(config_id, new_name):
        called["args"] = (config_id, new_name)
        return cfg

    monkeypatch.setattr(handlers, "get_or_create_user", fake_get_user)
    monkeypatch.setattr(handlers_base, "get_or_create_user", fake_get_user)
    monkeypatch.setattr(handlers.configs, "get_or_create_user", fake_get_user)
    monkeypatch.setattr(handlers.config_service, "get", fake_get)
    monkeypatch.setattr(handlers.config_service, "rename_config", fake_rename)

    await handlers.got_new_name(msg, state)

    assert called["args"] == (5, "new")
    assert state.cleared
    assert "Конфигурация переименована" in msg.answers[-1]
