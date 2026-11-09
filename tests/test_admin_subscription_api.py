"""The admin plan API changes assignments without moving money or provisioning."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from test_admin_product import _headers, _principal

from admin.request_context import RequestContextMiddleware
from admin.routers import admin_v1_subscriptions
from admin.security import CSRF_COOKIE_NAME, get_admin_principal
from core.config import settings
from core.db.models import (
    AdminAuditEvent,
    AdminRole,
    LedgerEntry,
    Subscription,
    User,
    VPN_Config,
    VPNOperation,
)
from core.db.unit_of_work import uow
from core.services import ServerService, UserService


def _app(principal):
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    app.include_router(admin_v1_subscriptions.router)
    app.include_router(admin_v1_subscriptions.users_router)
    app.dependency_overrides[get_admin_principal] = lambda: principal
    return app


async def _counts(sessionmaker):
    async with sessionmaker() as session:
        return tuple(
            [
                int(await session.scalar(select(func.count()).select_from(model)) or 0)
                for model in (Subscription, VPNOperation, VPN_Config, LedgerEntry)
            ]
        )


@pytest.mark.asyncio
async def test_hourly_catalog_assignment_and_replay_preserve_finite_money(
    sessionmaker, monkeypatch
):
    monkeypatch.setattr(settings, "billing_enabled", False)
    target = await UserService(uow).register(84_001, balance="25.00")
    principal, csrf = await _principal(sessionmaker)
    path = f"/api/admin/v1/users/{target.id}/subscription"
    before_counts = await _counts(sessionmaker)
    app = _app(principal)
    body = {"plan_code": "basic", "reason": "Reviewed basic assignment"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://admin.test"
    ) as client:
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        catalog = await client.get("/api/admin/v1/subscriptions/plans")
        assert catalog.status_code == 200, catalog.text
        basic, plus = catalog.json()["items"]
        assert (basic["hourly_rate_rub"], plus["hourly_rate_rub"]) == ("0.07", "0.14")
        assert (basic["monthly_estimate_rub"], plus["monthly_estimate_rub"]) == (
            "50.40",
            "100.80",
        )
        assert basic["max_hysteria_sessions"] == 1
        assert plus["max_hysteria_sessions"] is None
        assert "period_days" not in basic
        assert (await client.get(path)).json() == {"subscription": None}

        assigned = await client.patch(
            path, headers=_headers(csrf, "admin-basic-assign"), json=body
        )
        assert assigned.status_code == 200, assigned.text
        first = assigned.json()
        assert first["plan_code"] == "basic"
        assert first["billing_paused"] is True
        assert first["next_charge_at"] is None
        assert first["unlimited_balance"] is False
        assert (
            not {"pending_plan_code", "current_period_start", "current_period_end"}
            & first.keys()
        )

        changed = await client.patch(
            path,
            headers=_headers(csrf, "admin-plus-assign"),
            json={
                "plan_code": "plus",
                "expected_version": first["version"],
                "reason": "Reviewed plus assignment",
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["plan_code"] == "plus"
        assert changed.json()["version"] == first["version"] + 1
        replay = await client.patch(
            path, headers=_headers(csrf, "admin-basic-assign"), json=body
        )
        assert replay.status_code == 200, replay.text
        assert replay.json() == {**first, "replayed": True}
        assert (await client.get(path)).json()["subscription"]["plan_code"] == "plus"

        conflict = await client.patch(
            path,
            headers=_headers(csrf, "admin-basic-assign"),
            json={**body, "plan_code": "plus"},
        )
        assert conflict.status_code == 409
        stale = await client.patch(
            path,
            headers=_headers(csrf, "admin-stale-version"),
            json={**body, "expected_version": first["version"]},
        )
        assert stale.status_code == 409
        filtered = await client.get("/api/admin/v1/subscriptions?plan_code=basic")
        assert filtered.json()["total"] == 0
        filtered = await client.get(
            "/api/admin/v1/subscriptions?plan_code=plus&status=pending"
        )
        assert filtered.json()["total"] == 1
        assert (
            await client.get("/api/admin/v1/subscriptions?plan_code=invalid")
        ).status_code == 422

    after_counts = await _counts(sessionmaker)
    assert after_counts == (before_counts[0] + 1, *before_counts[1:])
    async with sessionmaker() as session:
        user = await session.get(User, target.id)
        assert str(user.balance) == "25.00"
        assert user.unlimited_balance is False
        audits = list(
            await session.scalars(
                select(AdminAuditEvent).where(
                    AdminAuditEvent.action == "subscription.plan_changed"
                )
            )
        )
        assert len(audits) == 2
        assert all(
            not item.details["wallet_changed"] and not item.details["ledger_changed"]
            for item in audits
        )


@pytest.mark.asyncio
async def test_plan_and_audit_commit_atomically(sessionmaker, monkeypatch):
    target = await UserService(uow).register(84_002)
    principal, csrf = await _principal(sessionmaker)

    def fail_audit(*args, **kwargs):
        raise RuntimeError("Simulated audit persistence outage")

    monkeypatch.setattr(admin_v1_subscriptions, "add_audit_event", fail_audit)
    async with AsyncClient(
        transport=ASGITransport(app=_app(principal), raise_app_exceptions=False),
        base_url="https://admin.test",
    ) as client:
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        response = await client.patch(
            f"/api/admin/v1/users/{target.id}/subscription",
            headers=_headers(csrf),
            json={"plan_code": "plus", "reason": "Reviewed assignment"},
        )
        assert response.status_code == 500
    async with sessionmaker() as session:
        assert (
            await session.scalar(
                select(Subscription).where(Subscription.user_id == target.id)
            )
            is None
        )


@pytest.mark.asyncio
async def test_ops_subscription_reads_omit_account_financial_exceptions(sessionmaker):
    from core.services.subscriptions import SubscriptionService

    target = await UserService(uow).register(84_003, balance="35.00")
    await SubscriptionService(uow).select_plan(target.id, "plus")
    async with sessionmaker() as session, session.begin():
        user = await session.get(User, target.id)
        user.unlimited_balance = True
    principal, _ = await _principal(sessionmaker, AdminRole.OPS)
    async with AsyncClient(
        transport=ASGITransport(app=_app(principal)), base_url="https://admin.test"
    ) as client:
        user_response = await client.get(
            f"/api/admin/v1/users/{target.id}/subscription"
        )
        listed = await client.get("/api/admin/v1/subscriptions")
        assert user_response.status_code == listed.status_code == 200
        for view in (user_response.json()["subscription"], listed.json()["items"][0]):
            assert view["plan_code"] == "plus"
            assert "unlimited_balance" not in view
            assert "next_charge_at" not in view
            assert "balance" not in view


@pytest.mark.asyncio
async def test_migration_preview_is_readonly_and_suspended_access_stays_off(
    sessionmaker, monkeypatch
):
    monkeypatch.setattr(settings, "billing_enabled", False)
    users = [await UserService(uow).register(84_100 + index) for index in range(4)]
    server = await ServerService(uow).create(
        name="migration-api-node",
        ip="192.0.2.91",
        port=443,
        host="vpn.example.test",
        location="NL",
        api_key="test-key",
        cost=10,
    )
    async with sessionmaker() as session, session.begin():
        for index, states in enumerate(
            ([], ["active"], ["active", "active"], ["suspended"])
        ):
            for number, state in enumerate(states):
                session.add(
                    VPN_Config(
                        owner_id=users[index].id,
                        server_id=server.id,
                        name=f"migration-api-{index}-{number}",
                        display_name="Legacy key",
                        desired_state=state,
                        actual_state=state,
                        suspended=state == "suspended",
                    )
                )
    before = await _counts(sessionmaker)
    principal, _ = await _principal(sessionmaker, AdminRole.FINANCE)
    async with AsyncClient(
        transport=ASGITransport(app=_app(principal)), base_url="https://admin.test"
    ) as client:
        response = await client.get("/api/admin/v1/subscriptions/migration/preview")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["read_only"] is True
        assert body["billing_enabled"] is False
        assert body["counts"] == {
            "basic": 1,
            "plus": 1,
            "suspended": 1,
            "unchanged": 1,
            "total": 4,
        }
        suspended = next(
            item for item in body["items"] if item["user_id"] == users[3].id
        )
        assert suspended["status"] == "suspended"
        assert suspended["active_configs"] == 0
        assert (
            body["balance_changes"]
            == body["ledger_changes"]
            == body["unlimited_balance_changes"]
            == 0
        )
        # There is deliberately no web apply route in this preparation.
        assert (
            await client.post("/api/admin/v1/subscriptions/migration/apply")
        ).status_code == 404
    assert await _counts(sessionmaker) == before
