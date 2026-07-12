import importlib

import bcrypt
import pytest
import fakeredis.aioredis
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
    password = b"secret"
    hashed = bcrypt.hashpw(password, bcrypt.gensalt()).decode()
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hashed)

    import core.config as core_config
    core_config = importlib.reload(core_config)
    import admin.auth as admin_auth
    admin_auth = importlib.reload(admin_auth)
    redis_client = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(admin_auth, "_get_redis", lambda: redis_client)
    import admin.app as admin_app
    admin_app = importlib.reload(admin_app)

    transport = ASGITransport(app=admin_app.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post("/login", json={"username": "admin", "password": "secret"})
        token = login.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        # create users via API
        resp = await client.post("/api/users", json={"tg_id": 1, "username": "alice"}, headers=headers)
        assert resp.status_code == 200
        alice = resp.json()

        resp = await client.post("/api/users", json={"tg_id": 2, "username": "bob"}, headers=headers)
        assert resp.status_code == 200

        # list with filter
        resp = await client.get("/api/users", params={"username": "alice"}, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1 and data[0]["username"] == "alice"

        # update user
        resp = await client.patch(
            f"/api/users/{alice['id']}", json={"username": "ally"}, headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["username"] == "ally"


@pytest.mark.asyncio
async def test_config_list(monkeypatch, sessionmaker):
    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *a, **kw: DummyGateway()
    )

    password = b"secret"
    hashed = bcrypt.hashpw(password, bcrypt.gensalt()).decode()
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hashed)

    import core.config as core_config
    core_config = importlib.reload(core_config)
    import admin.auth as admin_auth
    admin_auth = importlib.reload(admin_auth)
    redis_client = fakeredis.aioredis.FakeRedis()
    monkeypatch.setattr(admin_auth, "_get_redis", lambda: redis_client)
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
        login = await client.post("/login", json={"username": "admin", "password": "secret"})
        token = login.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        resp = await client.get("/api/configs", params={"owner_id": user.id}, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1 and data[0]["id"] == cfg.id
