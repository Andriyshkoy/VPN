import importlib

import pytest
from httpx import AsyncClient, ASGITransport

from core.db.unit_of_work import uow
from core.services import BillingService, ConfigService, ServerService, UserService


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


@pytest.mark.asyncio
async def test_user_endpoints(monkeypatch, sessionmaker):
    # reload app after patching DB session
    import admin.app as admin_app

    admin_app = importlib.reload(admin_app)

    transport = ASGITransport(app=admin_app.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # create users via API
        resp = await client.post("/api/users", json={"tg_id": 1, "username": "alice"})
        assert resp.status_code == 200
        alice = resp.json()

        resp = await client.post("/api/users", json={"tg_id": 2, "username": "bob"})
        assert resp.status_code == 200

        # list with filter
        resp = await client.get("/api/users", params={"username": "alice"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1 and data[0]["username"] == "alice"

        # update user
        resp = await client.patch(
            f"/api/users/{alice['id']}", json={"username": "ally"}
        )
        assert resp.status_code == 200
        assert resp.json()["username"] == "ally"


@pytest.mark.asyncio
async def test_config_list(monkeypatch, sessionmaker):
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *a, **kw: DummyGateway()
    )

    import admin.app as admin_app

    admin_app = importlib.reload(admin_app)

    server_svc = ServerService(uow)
    user_svc = UserService(uow)
    billing = BillingService(uow, per_config_cost=1)

    user = await user_svc.register(10)
    server = await server_svc.create(
        name="srv",
        ip="1.1.1.1",
        port=22,
        host="host",
        location="US",
        api_key="k",
        cost=1,
    )
    await billing.top_up(user.id, 10)
    cfg = await billing.create_paid_config(
        server_id=server.id,
        owner_id=user.id,
        name="cfg",
        display_name="disp",
        creation_cost=1,
    )

    transport = ASGITransport(app=admin_app.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/configs", params={"owner_id": user.id})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1 and data[0]["id"] == cfg.id
