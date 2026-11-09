"""Administrative product changes preserve money and require explicit grants."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from admin.request_context import RequestContextMiddleware
from admin.routers.admin_v1_subscriptions import users_router
from admin.routers.admin_v1_users import router as user_queries_router
from admin.security import CSRF_COOKIE_NAME, AdminPrincipal, get_admin_principal
from core.db.models import AdminAuditEvent, AdminRole, AdminUser, LedgerEntry, User
from core.db.unit_of_work import uow
from core.services import UserService


async def _principal(sessionmaker, role: AdminRole = AdminRole.OWNER):
    async with sessionmaker() as session, session.begin():
        actor = AdminUser(
            username=f"product-{role.value}",
            password_hash="$2b$12$unused-but-never-authenticated",
            role=role.value,
        )
        session.add(actor)
        await session.flush()
    csrf = "product-test-csrf-token"
    return (
        AdminPrincipal(
            user_id=actor.id,
            username=actor.username,
            role=role,
            session_id=1,
            csrf_token_hash=hashlib.sha256(csrf.encode()).hexdigest(),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ),
        csrf,
    )


def _app(principal: AdminPrincipal) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    app.include_router(users_router)
    app.include_router(user_queries_router)
    app.dependency_overrides[get_admin_principal] = lambda: principal
    return app


def _headers(csrf: str, key: str = "admin-product-toggle-01") -> dict[str, str]:
    return {"X-CSRF-Token": csrf, "Idempotency-Key": key}


def _toggle(enabled: bool = True, expected_enabled: bool = False, **extra):
    return {
        "enabled": enabled,
        "expected_enabled": expected_enabled,
        "reason": "Verified manual account exemption",
        **extra,
    }


async def _wallet_snapshot(sessionmaker, user_id: int):
    async with sessionmaker() as session:
        user = await session.get(User, user_id)
        ledger = list(
            (
                await session.scalars(
                    select(LedgerEntry)
                    .where(LedgerEntry.user_id == user_id)
                    .order_by(LedgerEntry.id)
                )
            ).all()
        )
        return user.balance, [
            (entry.id, entry.amount, entry.kind, entry.idempotency_key, entry.details)
            for entry in ledger
        ]


@pytest.mark.asyncio
async def test_manual_unlimited_toggle_preserves_wallet_and_replays_original_result(
    sessionmaker,
):
    target = await UserService(uow).register(81_001, balance="12.34")
    before = await _wallet_snapshot(sessionmaker, target.id)
    assert before[1], "Exercise an existing append-only ledger, not an empty one"
    principal, csrf = await _principal(sessionmaker)
    path = f"/api/admin/v1/users/{target.id}/unlimited-balance"

    async with AsyncClient(
        transport=ASGITransport(app=_app(principal)), base_url="https://admin.test"
    ) as client:
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        response = await client.patch(path, headers=_headers(csrf), json=_toggle())
        assert response.status_code == 200, response.text
        enabled = response.json()
        assert enabled["user_id"] == target.id
        assert enabled["unlimited_balance"] is True
        assert enabled["balance"] == "12.34"
        assert enabled["balance_display"] == "∞"
        assert enabled["replayed"] is False

        disabled = await client.patch(
            path,
            headers=_headers(csrf, "admin-product-disable-02"),
            json=_toggle(False, True),
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["unlimited_balance"] is False
        assert disabled.json()["balance_display"] == "12.34"

        replay = await client.patch(path, headers=_headers(csrf), json=_toggle())
        assert replay.status_code == 200, replay.text
        assert replay.json() == {**enabled, "replayed": True}

    async with sessionmaker() as session:
        stored = await session.get(User, target.id)
        assert stored.unlimited_balance is False
        assert stored.exclude_from_business_metrics is False
        audit = list(
            (
                await session.scalars(
                    select(AdminAuditEvent).where(
                        AdminAuditEvent.target_type == "user",
                        AdminAuditEvent.target_id == str(target.id),
                    )
                )
            ).all()
        )
        successful = [
            event for event in audit if event.details.get("outcome") != "rejected"
        ]
        assert len(successful) == 2
        assert all(event.actor_user_id == principal.user_id for event in successful)
        assert all(
            "Verified manual account exemption" in str(event.details)
            for event in successful
        )
    assert await _wallet_snapshot(sessionmaker, target.id) == before


@pytest.mark.asyncio
async def test_unlimited_toggle_rejects_reused_key_and_stale_expected_flag(
    sessionmaker,
):
    target = await UserService(uow).register(81_002, balance="5.00")
    before = await _wallet_snapshot(sessionmaker, target.id)
    principal, csrf = await _principal(sessionmaker)
    path = f"/api/admin/v1/users/{target.id}/unlimited-balance"
    async with AsyncClient(
        transport=ASGITransport(app=_app(principal)), base_url="https://admin.test"
    ) as client:
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        first = await client.patch(path, headers=_headers(csrf), json=_toggle())
        assert first.status_code == 200, first.text
        conflicting = await client.patch(
            path, headers=_headers(csrf), json=_toggle(False, True)
        )
        assert conflicting.status_code == 409
        stale = await client.patch(
            path,
            headers=_headers(csrf, "admin-product-stale-03"),
            json=_toggle(False, False),
        )
        assert stale.status_code == 409
    async with sessionmaker() as session:
        assert (await session.get(User, target.id)).unlimited_balance is True
    assert await _wallet_snapshot(sessionmaker, target.id) == before


@pytest.mark.parametrize(
    ("role", "expected_status"),
    [
        (AdminRole.OWNER, 200),
        (AdminRole.FINANCE, 200),
        (AdminRole.SUPPORT, 403),
        (AdminRole.OPS, 403),
        (AdminRole.VIEWER, 403),
    ],
)
@pytest.mark.asyncio
async def test_unlimited_toggle_requires_financial_permission(
    sessionmaker, role, expected_status
):
    target = await UserService(uow).register(81_003, balance="7.89")
    before = await _wallet_snapshot(sessionmaker, target.id)
    principal, csrf = await _principal(sessionmaker, role)
    async with AsyncClient(
        transport=ASGITransport(app=_app(principal)), base_url="https://admin.test"
    ) as client:
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        response = await client.patch(
            f"/api/admin/v1/users/{target.id}/unlimited-balance",
            headers=_headers(csrf),
            json=_toggle(),
        )
    assert response.status_code == expected_status, response.text
    async with sessionmaker() as session:
        assert (await session.get(User, target.id)).unlimited_balance is (
            expected_status == 200
        )
    assert await _wallet_snapshot(sessionmaker, target.id) == before


@pytest.mark.parametrize(
    "csrf_mode", ["absent", "header_only", "mismatch", "cross_origin"]
)
@pytest.mark.asyncio
async def test_unlimited_toggle_requires_csrf_and_same_origin(sessionmaker, csrf_mode):
    target = await UserService(uow).register(81_004)
    principal, csrf = await _principal(sessionmaker)
    async with AsyncClient(
        transport=ASGITransport(app=_app(principal)), base_url="https://admin.test"
    ) as client:
        headers = {"Idempotency-Key": "admin-product-csrf-01"}
        if csrf_mode != "absent":
            headers["X-CSRF-Token"] = csrf
        if csrf_mode in {"mismatch", "cross_origin"}:
            client.cookies.set(
                CSRF_COOKIE_NAME, "different-token" if csrf_mode == "mismatch" else csrf
            )
        if csrf_mode == "cross_origin":
            headers["Origin"] = "https://other.test"
        response = await client.patch(
            f"/api/admin/v1/users/{target.id}/unlimited-balance",
            headers=headers,
            json=_toggle(),
        )
    assert response.status_code == 403
    async with sessionmaker() as session:
        assert (await session.get(User, target.id)).unlimited_balance is False


@pytest.mark.parametrize(
    "invalid_fields",
    [
        {"reason": "   "},
        {"reason": "x"},
        {"enabled": "false"},
        {"expected_enabled": "false"},
        {"balance": "Infinity"},
    ],
)
@pytest.mark.asyncio
async def test_unlimited_toggle_rejects_ambiguous_or_unauditable_input(
    sessionmaker, invalid_fields
):
    target = await UserService(uow).register(81_005, balance="1.23")
    principal, csrf = await _principal(sessionmaker)
    async with AsyncClient(
        transport=ASGITransport(app=_app(principal)), base_url="https://admin.test"
    ) as client:
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        response = await client.patch(
            f"/api/admin/v1/users/{target.id}/unlimited-balance",
            headers=_headers(csrf),
            json=_toggle(**invalid_fields),
        )
    assert response.status_code == 422, response.text
    async with sessionmaker() as session:
        stored = await session.get(User, target.id)
        assert stored.unlimited_balance is False
        assert stored.balance == Decimal("1.23")


@pytest.mark.asyncio
async def test_unlimited_toggle_unknown_account_returns_not_found(sessionmaker):
    principal, csrf = await _principal(sessionmaker)
    async with AsyncClient(
        transport=ASGITransport(app=_app(principal)), base_url="https://admin.test"
    ) as client:
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        response = await client.patch(
            "/api/admin/v1/users/999999/unlimited-balance",
            headers=_headers(csrf),
            json=_toggle(),
        )
    assert response.status_code == 404


@pytest.mark.parametrize("key", [None, "short", "x" * 161])
@pytest.mark.asyncio
async def test_unlimited_toggle_requires_bounded_idempotency_key(sessionmaker, key):
    target = await UserService(uow).register(81_008)
    principal, csrf = await _principal(sessionmaker)
    headers = {"X-CSRF-Token": csrf}
    if key is not None:
        headers["Idempotency-Key"] = key
    async with AsyncClient(
        transport=ASGITransport(app=_app(principal)), base_url="https://admin.test"
    ) as client:
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        response = await client.patch(
            f"/api/admin/v1/users/{target.id}/unlimited-balance",
            headers=headers,
            json=_toggle(),
        )
    assert response.status_code == 422, response.text
    async with sessionmaker() as session:
        assert (await session.get(User, target.id)).unlimited_balance is False


@pytest.mark.parametrize("telegram_id", [810_001, 810_002, 81_006])
@pytest.mark.asyncio
async def test_no_account_receives_automatic_unlimited_balance(
    sessionmaker, telegram_id
):
    target = await UserService(uow).register(telegram_id)
    async with sessionmaker() as session:
        stored = await session.get(User, target.id)
        assert stored.unlimited_balance is False
        assert stored.exclude_from_business_metrics is False


@pytest.mark.asyncio
async def test_admin_user_reads_keep_finite_money_and_display_unlimited_separately(
    sessionmaker,
):
    target = await UserService(uow).register(81_007, balance="19.99")
    async with sessionmaker() as session, session.begin():
        (await session.get(User, target.id)).unlimited_balance = True
    principal, _ = await _principal(sessionmaker)
    async with AsyncClient(
        transport=ASGITransport(app=_app(principal)), base_url="https://admin.test"
    ) as client:
        detail = await client.get(f"/api/admin/v1/users/{target.id}")
        page = await client.get("/api/admin/v1/users", params={"q": str(target.tg_id)})
    assert detail.status_code == page.status_code == 200
    payload = detail.json()
    assert payload["unlimited_balance"] is True
    assert payload["exclude_from_business_metrics"] is False
    assert payload["finance"]["balance"] == "19.99"
    assert payload["finance"]["balance_display"] == "∞"
    item = page.json()["items"][0]
    assert item["balance"] == "19.99"
    assert item["unlimited_balance"] is True
    assert item["exclude_from_business_metrics"] is False
