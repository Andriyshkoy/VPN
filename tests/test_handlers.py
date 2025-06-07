import os
import types
import tempfile
import pytest

import bot.handlers as handlers

class DummyMessage:
    def __init__(self, text):
        self.text = text
        self.from_user = types.SimpleNamespace(id=1, username="user")
        self.chat = types.SimpleNamespace(id=123)
        self.answers = []
    async def answer(self, text):
        self.answers.append(text)

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
async def test_tempfile_used(monkeypatch):
    msg = DummyMessage("../../etc/passwd")
    state = DummyState()
    bot = DummyBot()

    async def fake_get_user(tg_id, username=None):
        return types.SimpleNamespace(id=1)
    async def fake_create_config(server_id, owner_id, name, display_name):
        return types.SimpleNamespace(id=5)
    async def fake_download_config(cfg_id):
        return b"data"

    monkeypatch.setattr(handlers, "get_or_create_user", fake_get_user)
    monkeypatch.setattr(handlers.config_service, "create_config", fake_create_config)
    monkeypatch.setattr(handlers.config_service, "download_config", fake_download_config)
    monkeypatch.setattr(handlers, "FSInputFile", DummyFSInputFile)

    await handlers.got_name(msg, state, bot)

    sent_path = bot.sent.path
    tmp_dir = tempfile.gettempdir()
    assert os.path.commonpath([sent_path, tmp_dir]) == tmp_dir
    assert not os.path.exists(sent_path)
    assert state.cleared
    assert msg.answers[-1] == "Config created"


@pytest.mark.asyncio
async def test_service_error(monkeypatch):
    msg = DummyMessage("name")
    state = DummyState()
    bot = DummyBot()

    async def fake_get_user(tg_id, username=None):
        return types.SimpleNamespace(id=1)

    async def fake_create_config(*a, **kw):
        raise handlers.ServiceError("boom")

    monkeypatch.setattr(handlers, "get_or_create_user", fake_get_user)
    monkeypatch.setattr(handlers.config_service, "create_config", fake_create_config)

    await handlers.got_name(msg, state, bot)

    assert state.cleared
    assert msg.answers[-1] == "Произошла ошибка. Попробуйте позже"


class DummyMessageReply:
    def __init__(self):
        self.chat = types.SimpleNamespace(id=123)
        self.answers = []

    async def answer(self, text, reply_markup=None):
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
    async def fake_get(*a, **kw):
        return cfg

    async def fake_server(*a, **kw):
        return server

    monkeypatch.setattr(handlers.config_service, "get", fake_get)
    monkeypatch.setattr(handlers.server_service, "get", fake_server)

    await handlers.show_config(cb)

    markup = cb.message.answers[0][1]
    button_texts = [b.text for row in markup.inline_keyboard for b in row]
    assert "Download" in button_texts
    assert "Rename" in button_texts


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
    async def fake_get_config(*a, **kw):
        return cfg

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


@pytest.mark.asyncio
async def test_rename_callback_sets_state(monkeypatch):
    cb = DummyCallback("rn:3")
    state = DummyStateRename()

    await handlers.rename_config_cb(cb, state)

    assert state.data["config_id"] == 3
    assert state.state == handlers.RenameConfig.entering_name
    assert cb.message.answers[-1][0] == "Send new display name"


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
    monkeypatch.setattr(handlers.config_service, "get", fake_get)
    monkeypatch.setattr(handlers.config_service, "rename_config", fake_rename)

    await handlers.got_new_name(msg, state)

    assert called["args"] == (5, "new")
    assert state.cleared
    assert msg.answers[-1] == "Config renamed"
