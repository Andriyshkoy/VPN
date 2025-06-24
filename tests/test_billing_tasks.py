import importlib
import types

import pytest

from core.db.unit_of_work import uow
from core.services import BillingService, ServerService, UserService


@pytest.mark.asyncio
async def test_charge_and_notify(monkeypatch, sessionmaker):
    class DummyGateway:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

        async def create_client(self, name, use_password=False):
            pass

        async def download_config(self, name):
            return b"data"

        async def revoke_client(self, name):
            pass

        async def suspend_client(self, name):
            pass

        async def unsuspend_client(self, name):
            pass

        async def list_blocked(self):
            return []

    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *a, **kw: DummyGateway()
    )
    monkeypatch.setenv("BOT_TOKEN", "token:1")
    import core.config as core_config

    core_config = importlib.reload(core_config)

    import billing_daemon.billing_tasks as billing_tasks

    billing_tasks = importlib.reload(billing_tasks)

    sent = []

    class DummyBot:
        def __init__(self, *a, **kw):
            pass

        async def send_message(self, chat_id, text):
            sent.append((chat_id, text))

        @property
        def session(self):
            class S:
                async def close(self):
                    pass

            return S()

    monkeypatch.setattr(billing_tasks, "Bot", DummyBot)

    user_svc = UserService(uow)
    server_svc = ServerService(uow)
    billing = BillingService(uow, per_config_cost=1)

    user = await user_svc.register(123)
    server = await server_svc.create(
        name="s",
        ip="1",
        port=22,
        host="h",
        location="loc",
        api_key="k",
        cost=1,
    )

    await billing.top_up(user.id, 9)
    await billing.create_paid_config(
        server_id=server.id,
        owner_id=user.id,
        name="cfg",
        display_name="d",
        creation_cost=5,
    )

    await billing_tasks._charge_all_and_notify_async()

    assert sent and str(user.tg_id) in str(sent[0][0])
