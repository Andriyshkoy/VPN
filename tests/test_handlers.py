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
