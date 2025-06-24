import importlib

import bcrypt
import pytest
import fakeredis.aioredis
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_login(monkeypatch, sessionmaker):
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
        resp = await client.post("/login", json={"username": "admin", "password": "secret"})
        assert resp.status_code == 200
        token = resp.json()["token"]

        # token works for protected endpoint
        resp = await client.get("/api/users", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200

        # wrong token rejected
        resp = await client.get("/api/users", headers={"Authorization": "Bearer bad"})
        assert resp.status_code == 401
