from __future__ import annotations

import json
import os
import subprocess
import sys

import bcrypt
import pytest
from fastapi import Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from admin.app import app
from admin.request_context import RequestContextMiddleware
from admin.routers.auth_v1 import router as auth_v1_router
from admin.security import (
    AdminPermission,
    AdminPrincipal,
    AdminRole,
    _client_ip,
    login_rate_limiter,
    permissions_for_role,
    require_permission,
)
from core.config import Settings
from core.db.models import AdminAuditEvent, AdminSession, AdminUser


def _legacy_credentials(monkeypatch) -> None:
    password_hash = bcrypt.hashpw(b"correct horse", bcrypt.gensalt()).decode()
    monkeypatch.setenv("ADMIN_USERNAME", "Legacy-Owner")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", password_hash)


def _client(target_app=app) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=target_app),
        base_url="https://admin.test",
        headers={"Origin": "https://admin.test"},
    )


@pytest.mark.asyncio
async def test_admin_api_does_not_allow_credentialed_sibling_origin_reads():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://admin.vpn.andriyshkoy.ru",
        headers={"Origin": "https://vpn.andriyshkoy.ru"},
    ) as client:
        response = await client.get("/api/admin/v1/auth/me")

    assert response.status_code == 401
    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-credentials" not in response.headers


def _proxy_request(*, peer: str, forwarded_for: str | None = None) -> Request:
    headers = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode("ascii")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/api/admin/v1/auth/login",
            "raw_path": b"/api/admin/v1/auth/login",
            "query_string": b"",
            "headers": headers,
            "client": (peer, 42_000),
            "server": ("admin.test", 443),
        }
    )


def _proxy_settings(cidrs: str) -> Settings:
    return Settings().model_copy(update={"admin_trusted_proxy_cidrs": cidrs})


def _registered_admin_paths(*, legacy_enabled: bool) -> set[str]:
    environment = os.environ.copy()
    environment["ADMIN_LEGACY_API_ENABLED"] = str(legacy_enabled).lower()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; "
                "from admin.app import app; "
                "print(json.dumps(sorted(app.openapi()['paths'])))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return set(json.loads(result.stdout.strip()))


@pytest.mark.parametrize("legacy_enabled", [False, True])
def test_legacy_admin_routes_follow_explicit_rollback_flag(legacy_enabled):
    paths = _registered_admin_paths(legacy_enabled=legacy_enabled)
    legacy_paths = {"/login", "/api/users", "/api/configs", "/api/servers"}

    assert legacy_paths.issubset(paths) is legacy_enabled
    assert "/api/admin/v1/auth/login" in paths


def test_direct_untrusted_peer_cannot_spoof_forwarded_client_ip():
    request = _proxy_request(
        peer="203.0.113.10",
        forwarded_for="198.51.100.77",
    )

    assert _client_ip(request, _proxy_settings("172.16.0.0/12")) == "203.0.113.10"


def test_trusted_nginx_uses_real_peer_and_ignores_attacker_prefix():
    request = _proxy_request(
        peer="172.20.0.3",
        forwarded_for="198.51.100.77, 203.0.113.10",
    )

    assert _client_ip(request, _proxy_settings("172.16.0.0/12")) == "203.0.113.10"


def test_two_explicitly_trusted_proxies_resolve_original_client():
    request = _proxy_request(
        peer="172.20.0.3",
        forwarded_for="203.0.113.10, 10.42.0.8",
    )

    settings = _proxy_settings("172.16.0.0/12,10.42.0.0/16")
    assert _client_ip(request, settings) == "203.0.113.10"


@pytest.mark.parametrize(
    "forwarded_for",
    [
        "not-an-ip",
        ", ".join(f"10.0.0.{index}" for index in range(1, 12)),
    ],
)
def test_invalid_or_oversized_forwarded_chain_falls_back_to_direct_peer(
    forwarded_for,
):
    request = _proxy_request(peer="172.20.0.3", forwarded_for=forwarded_for)

    assert _client_ip(request, _proxy_settings("172.16.0.0/12")) == "172.20.0.3"


def test_invalid_trusted_proxy_cidr_rejects_settings_startup(monkeypatch):
    monkeypatch.setenv("ADMIN_TRUSTED_PROXY_CIDRS", "172.16.0.0/12,not-a-cidr")

    with pytest.raises(ValueError, match="ADMIN_TRUSTED_PROXY_CIDRS"):
        Settings()


@pytest.mark.asyncio
async def test_legacy_owner_bootstrap_session_csrf_logout_and_audit(
    monkeypatch, sessionmaker
):
    _legacy_credentials(monkeypatch)
    await login_rate_limiter.clear()

    async with _client() as client:
        login = await client.post(
            "/api/admin/v1/auth/login",
            json={"username": " LEGACY-owner ", "password": "correct horse"},
            headers={
                "X-Request-ID": "req-login-1",
                "X-Correlation-ID": "corr-security-test",
            },
        )
        assert login.status_code == 200
        body = login.json()
        assert body["actor"]["username"] == "legacy-owner"
        assert body["actor"]["role"] == "owner"
        assert AdminPermission.ADMINS_MANAGE.value in body["actor"]["permissions"]
        assert login.headers["X-Request-ID"] == "req-login-1"
        assert login.headers["X-Correlation-ID"] == "corr-security-test"

        set_cookies = login.headers.get_list("set-cookie")
        session_cookie = next(
            value for value in set_cookies if value.startswith("vpn_admin_session=")
        )
        csrf_cookie = next(
            value for value in set_cookies if value.startswith("vpn_admin_csrf=")
        )
        assert "HttpOnly" in session_cookie
        assert "Secure" in session_cookie
        assert "SameSite=strict" in session_cookie
        assert "Path=/api/admin/v1" in session_cookie
        assert "HttpOnly" not in csrf_cookie
        assert "Secure" in csrf_cookie
        assert "SameSite=strict" in csrf_cookie
        assert "Path=/" in csrf_cookie

        me = await client.get("/api/admin/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["actor"] == body["actor"]
        assert me.json()["csrf_token"] == body["csrf_token"]

        missing_csrf = await client.post("/api/admin/v1/auth/logout")
        assert missing_csrf.status_code == 403

        logout = await client.post(
            "/api/admin/v1/auth/logout",
            headers={"X-CSRF-Token": body["csrf_token"]},
        )
        assert logout.status_code == 204
        after_logout = await client.get("/api/admin/v1/auth/me")
        assert after_logout.status_code == 401

    async with sessionmaker() as session:
        user = await session.scalar(select(AdminUser))
        assert user is not None
        assert user.role == AdminRole.OWNER.value
        assert user.password_hash.startswith("$2")
        db_session = await session.scalar(select(AdminSession))
        assert db_session is not None
        # Raw opaque secrets never reach persistent storage.
        assert body["csrf_token"] not in {
            db_session.token_hash,
            db_session.csrf_token_hash,
        }
        events = (
            await session.scalars(select(AdminAuditEvent).order_by(AdminAuditEvent.id))
        ).all()
        assert [event.action for event in events] == [
            "admin.login_succeeded",
            "admin.logout",
        ]
        assert events[0].request_id == "req-login-1"
        assert events[0].correlation_id == "corr-security-test"
        assert events[0].details == {"legacy_owner_bootstrap": True}


@pytest.mark.asyncio
async def test_logout_all_revokes_every_browser_session(monkeypatch, sessionmaker):
    _legacy_credentials(monkeypatch)
    await login_rate_limiter.clear()

    async with _client() as first, _client() as second:
        first_login = await first.post(
            "/api/admin/v1/auth/login",
            json={"username": "legacy-owner", "password": "correct horse"},
        )
        second_login = await second.post(
            "/api/admin/v1/auth/login",
            json={"username": "legacy-owner", "password": "correct horse"},
        )
        assert first_login.status_code == second_login.status_code == 200

        logout_all = await first.post(
            "/api/admin/v1/auth/logout-all",
            headers={"X-CSRF-Token": first_login.json()["csrf_token"]},
        )
        assert logout_all.status_code == 204
        assert (await second.get("/api/admin/v1/auth/me")).status_code == 401

    async with sessionmaker() as session:
        active_sessions = await session.scalar(
            select(func.count(AdminSession.id)).where(AdminSession.revoked_at.is_(None))
        )
        assert active_sessions == 0


@pytest.mark.asyncio
async def test_account_lockout_persists_failed_logins(monkeypatch, sessionmaker):
    _legacy_credentials(monkeypatch)
    monkeypatch.setenv("ADMIN_LOGIN_MAX_FAILURES", "3")
    monkeypatch.setenv("ADMIN_LOGIN_LOCKOUT_SECONDS", "60")
    await login_rate_limiter.clear()

    async with _client() as client:
        bootstrap = await client.post(
            "/api/admin/v1/auth/login",
            json={"username": "legacy-owner", "password": "correct horse"},
        )
        assert bootstrap.status_code == 200

        for _ in range(3):
            failed = await client.post(
                "/api/admin/v1/auth/login",
                json={"username": "legacy-owner", "password": "wrong"},
            )
            assert failed.status_code == 401

        blocked = await client.post(
            "/api/admin/v1/auth/login",
            json={"username": "legacy-owner", "password": "correct horse"},
        )
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) > 0

    async with sessionmaker() as session:
        user = await session.scalar(select(AdminUser))
        assert user.failed_login_attempts == 3
        assert user.locked_until is not None
        failed_audits = await session.scalar(
            select(func.count(AdminAuditEvent.id)).where(
                AdminAuditEvent.action == "admin.login_failed"
            )
        )
        assert failed_audits == 3


@pytest.mark.asyncio
async def test_login_rejects_cross_origin_before_bootstrap(monkeypatch, sessionmaker):
    _legacy_credentials(monkeypatch)
    await login_rate_limiter.clear()

    async with _client() as client:
        response = await client.post(
            "/api/admin/v1/auth/login",
            json={"username": "legacy-owner", "password": "correct horse"},
            headers={"Origin": "https://evil.example"},
        )
        assert response.status_code == 403

    async with sessionmaker() as session:
        assert await session.scalar(select(func.count(AdminUser.id))) == 0


def test_role_permissions_are_least_privilege_and_complete():
    assert permissions_for_role(AdminRole.OWNER) == frozenset(AdminPermission)
    assert AdminPermission.BALANCE_WRITE in permissions_for_role(AdminRole.FINANCE)
    assert AdminPermission.BALANCE_WRITE not in permissions_for_role(AdminRole.SUPPORT)
    assert AdminPermission.SERVERS_WRITE in permissions_for_role(AdminRole.OPS)
    assert AdminPermission.SERVERS_WRITE not in permissions_for_role(AdminRole.VIEWER)
    with pytest.raises(ValueError, match="Unknown admin role"):
        permissions_for_role("superuser")


@pytest.mark.asyncio
async def test_permission_dependency_enforces_role_and_csrf(sessionmaker):
    password_hash = bcrypt.hashpw(b"support-password", bcrypt.gensalt()).decode()
    async with sessionmaker() as session, session.begin():
        session.add(
            AdminUser(
                username="support",
                password_hash=password_hash,
                role=AdminRole.SUPPORT.value,
            )
        )

    test_app = FastAPI()
    test_app.add_middleware(RequestContextMiddleware)
    test_app.include_router(auth_v1_router)

    @test_app.get("/api/admin/v1/test/users")
    async def read_users(
        principal: AdminPrincipal = Depends(
            require_permission(AdminPermission.USERS_READ)
        ),
    ):
        return {"actor_id": principal.user_id}

    @test_app.get("/api/admin/v1/test/finance-write")
    async def finance_write(
        principal: AdminPrincipal = Depends(
            require_permission(AdminPermission.BALANCE_WRITE)
        ),
    ):
        return {"actor_id": principal.user_id}

    @test_app.post("/api/admin/v1/test/users")
    async def write_users(
        principal: AdminPrincipal = Depends(
            require_permission(AdminPermission.USERS_WRITE)
        ),
    ):
        return {"actor_id": principal.user_id}

    await login_rate_limiter.clear()
    async with _client(test_app) as client:
        login = await client.post(
            "/api/admin/v1/auth/login",
            json={"username": "support", "password": "support-password"},
        )
        assert login.status_code == 200
        csrf_token = login.json()["csrf_token"]

        assert (await client.get("/api/admin/v1/test/users")).status_code == 200
        assert (await client.get("/api/admin/v1/test/finance-write")).status_code == 403
        assert (await client.post("/api/admin/v1/test/users")).status_code == 403
        mutation = await client.post(
            "/api/admin/v1/test/users",
            headers={"X-CSRF-Token": csrf_token},
        )
        assert mutation.status_code == 200
        cross_origin = await client.post(
            "/api/admin/v1/test/users",
            headers={
                "X-CSRF-Token": csrf_token,
                "Origin": "https://evil.example",
            },
        )
        assert cross_origin.status_code == 403
