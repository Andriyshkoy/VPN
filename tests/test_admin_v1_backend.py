from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select, update
from starlette.requests import Request

from admin.request_context import RequestContextMiddleware
from admin.routers import admin_v1_configs, admin_v1_system, admin_v1_users
from admin.routers.admin_v1_configs import router as configs_router
from admin.routers.admin_v1_system import router as system_router
from admin.routers.admin_v1_users import router as users_router
from admin.schemas_v1 import BalanceAdjustmentRequest
from admin.security import (
    CSRF_COOKIE_NAME,
    CSRF_COOKIE_PATH,
    AdminPrincipal,
    get_admin_principal,
)
from admin.services_v1 import (
    AdminBalanceService,
    AdminIdempotencyConflict,
    AdminOptimisticConflict,
    BalanceAdjustmentCommand,
)
from core.db.models import (
    AdminAuditEvent,
    AdminRole,
    AdminUser,
    BillingRun,
    LedgerEntry,
    LedgerKind,
    NotificationOutbox,
    ProviderPayment,
    Server,
    TelegramUpdateInbox,
    User,
    VPN_Config,
    VPNOperation,
)
from core.db.unit_of_work import uow
from core.observability.manager_tls import ManagerTLSStatus
from core.services import BillingService, ServerService, UserService
from core.services.admin_queries import (
    AdminAnalyticsQueryService,
    AdminReferralQueryService,
    AdminUserQueryService,
    money,
)

NOW = datetime(2026, 7, 14, 4, 0, tzinfo=timezone.utc)


async def _admin_principal(
    sessionmaker,
    *,
    role: AdminRole = AdminRole.OWNER,
) -> tuple[AdminPrincipal, str]:
    async with sessionmaker() as session, session.begin():
        actor = AdminUser(
            username=role.value,
            password_hash="$2b$12$unused-but-never-authenticated",
            role=role.value,
        )
        session.add(actor)
        await session.flush()

    csrf_token = "test-csrf-token"
    return (
        AdminPrincipal(
            user_id=actor.id,
            username=actor.username,
            role=role,
            session_id=1,
            csrf_token_hash=hashlib.sha256(csrf_token.encode()).hexdigest(),
            expires_at=NOW + timedelta(hours=1),
        ),
        csrf_token,
    )


def _request(*, method: str = "POST", path: str = "/api/admin/v1/test") -> Request:
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"host", b"admin.test"), (b"user-agent", b"pytest")],
            "client": ("127.0.0.1", 12345),
            "server": ("admin.test", 443),
        }
    )
    request.state.request_id = "admin-v1-test-request"
    request.state.correlation_id = "admin-v1-test-correlation"
    return request


def _test_app(principal: AdminPrincipal, *routers) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    for router in routers:
        app.include_router(router)
    app.dependency_overrides[get_admin_principal] = lambda: principal
    return app


def test_money_contract_is_exact_and_rejects_ambiguous_json_numbers():
    assert money(Decimal("1.005")) == "1.01"
    assert money(Decimal("-1.005")) == "-1.01"
    assert money(None) == "0.00"

    parsed = BalanceAdjustmentRequest(
        direction="credit",
        amount="10.20",
        reason_code="support_correction",
        comment="  verified   support correction  ",
        expected_balance="-2.00",
    )
    assert parsed.amount == Decimal("10.20")
    assert parsed.expected_balance == Decimal("-2.00")
    assert parsed.comment == "verified support correction"

    with pytest.raises(ValidationError, match="JSON strings"):
        BalanceAdjustmentRequest(
            direction="credit",
            amount=10.2,
            reason_code="support_correction",
            comment="verified correction",
        )
    with pytest.raises(ValidationError, match="at most two decimal places"):
        BalanceAdjustmentRequest(
            direction="credit",
            amount="10.201",
            reason_code="support_correction",
            comment="verified correction",
        )


@pytest.mark.asyncio
async def test_user_360_filters_enrichment_and_snapshot_pagination(sessionmaker):
    users = UserService(uow)
    billing = BillingService(uow, per_config_cost="1.00")
    root = await users.register(71_001, username="root")
    target = await users.register(
        71_002,
        username="alice_vpn",
        referred_by_id=root.id,
        balance="12.34",
    )
    await users.register(71_003, username="no_activity")
    server = await ServerService(uow).create(
        name="test-server",
        ip="192.0.2.10",
        port=443,
        host="vpn.example.test",
        location="NL",
        api_key="manager-key",
        cost=30,
        lifecycle_state="active",
        accepts_new_configs=True,
    )
    await billing.top_up(target.id, "2.66", idempotency_key="admin-query:topup")
    await billing.withdraw(target.id, "1.00", idempotency_key="admin-query:withdraw")

    async with sessionmaker() as session, session.begin():
        session.add(
            VPN_Config(
                name="cfg-admin-query",
                display_name="Alice laptop",
                owner_id=target.id,
                server_id=server.id,
                desired_state="active",
                actual_state="active",
                suspended=False,
            )
        )
        session.add(
            ProviderPayment(
                intent_id="admin-query-payment",
                user_id=target.id,
                provider="telegram",
                provider_payment_id="charge-admin-query",
                amount=Decimal("50.10"),
                currency="RUB",
                payload="topup:admin-query-payment",
                status="credited",
                created_at=NOW,
                expires_at=NOW + timedelta(hours=1),
                credited_at=NOW,
            )
        )

    service = AdminUserQueryService(uow)
    page = await service.list_users(
        q=str(target.tg_id),
        has_configs=True,
        has_payments=True,
        limit=500,
        offset=-10,
    )
    assert page["total"] == 1
    assert page["limit"] == 100
    assert page["offset"] == 0
    assert page["items"][0] == {
        "id": target.id,
        "tg_id": target.tg_id,
        "username": "alice_vpn",
        "created_at": page["items"][0]["created_at"],
        "balance": "14.00",
        "delivery_status": "active",
        "blocked_at": None,
        "referrer": {"id": root.id, "username": "root"},
        "config_counts": {
            "total": 1,
            "active": 1,
            "suspended": 0,
            "pending": 0,
            "failed": 0,
        },
        "credited_total": "50.10",
        "last_payment_at": NOW.isoformat(),
        "direct_referrals": 0,
    }
    by_config = await service.list_users(
        q="Alice laptop",
        config_state="active",
    )
    assert by_config["total"] == 1
    assert by_config["items"][0]["id"] == target.id
    assert (await service.list_users(q="Alice laptop", config_state="suspended"))[
        "total"
    ] == 0

    user_360 = await service.get_user(target.id)
    assert user_360 is not None
    assert user_360["identity"]["username"] == "alice_vpn"
    assert user_360["finance"] == {
        "balance": "14.00",
        "latest_ledger_entry_id": user_360["finance"]["latest_ledger_entry_id"],
        "provider_deposits": "50.10",
        "service_charges": "0.00",
        "config_fees": "0.00",
        "config_refunds": "0.00",
        "referral_rewards": "0.00",
        "manual_adjustments": "1.66",
        "last_payment_at": NOW.isoformat(),
    }
    assert user_360["configs"] == {
        "total": 1,
        "active": 1,
        "suspended": 0,
        "pending": 0,
        "failed": 0,
    }
    assert user_360["referral"]["referrer"]["id"] == root.id

    first_snapshot = await service.list_ledger(target.id, limit=1)
    assert first_snapshot is not None
    assert first_snapshot["total"] == 3
    await billing.top_up(target.id, "3.00", idempotency_key="admin-query:later")
    stable_page = await service.list_ledger(
        target.id,
        limit=1,
        offset=1,
        snapshot_id=first_snapshot["snapshot_id"],
    )
    refreshed = await service.list_ledger(target.id)
    assert stable_page is not None and stable_page["total"] == 3
    assert stable_page["snapshot_id"] == first_snapshot["snapshot_id"]
    assert refreshed is not None and refreshed["total"] == 4
    assert refreshed["items"][0]["amount"] == "3.00"

    assert await service.get_user(999_999) is None
    assert await service.list_ledger(999_999) is None


@pytest.mark.asyncio
async def test_user_360_hides_finance_and_referrals_without_permissions(sessionmaker):
    principal, _ = await _admin_principal(sessionmaker, role=AdminRole.OPS)
    inviter = await UserService(uow).register(71_101, username="private-inviter")
    target = await UserService(uow).register(
        71_102,
        username="ops-visible-user",
        referred_by_id=inviter.id,
        balance="123.45",
    )
    app = _test_app(principal, users_router)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://admin.test",
    ) as client:
        response = await client.get(f"/api/admin/v1/users/{target.id}")

    assert response.status_code == 200
    payload = response.json()
    assert "finance" not in payload
    assert "referral" not in payload
    assert "referral_code" not in payload["identity"]
    assert payload["identity"]["username"] == "ops-visible-user"


@pytest.mark.asyncio
async def test_referral_ancestry_is_bounded_and_marks_corrupt_cycles(sessionmaker):
    users = UserService(uow)
    first = await users.register(72_001, username="first")
    second = await users.register(
        72_002,
        username="second",
        referred_by_id=first.id,
    )
    third = await users.register(
        72_003,
        username="third",
        referred_by_id=second.id,
    )

    # Simulate legacy/corrupt data that predates cycle validation.
    async with sessionmaker() as session, session.begin():
        await session.execute(
            update(User).where(User.id == first.id).values(referred_by_id=third.id)
        )

    ancestry = await AdminReferralQueryService(uow).ancestry(third.id)
    assert ancestry is not None
    assert [(item["id"], item["depth"], item["cycle"]) for item in ancestry] == [
        (second.id, 1, False),
        (first.id, 2, False),
        (third.id, 3, True),
    ]
    assert len(ancestry) == 3
    assert await AdminReferralQueryService(uow).ancestry(999_999) is None


@pytest.mark.asyncio
async def test_balance_adjustment_is_audited_idempotent_and_optimistic(sessionmaker):
    principal, _ = await _admin_principal(sessionmaker)
    customer = await UserService(uow).register(
        73_001,
        username="balance-user",
        balance="20.00",
    )
    async with sessionmaker() as session:
        opening_id = int(
            await session.scalar(
                select(func.max(LedgerEntry.id)).where(
                    LedgerEntry.user_id == customer.id
                )
            )
            or 0
        )

    service = AdminBalanceService()
    command = BalanceAdjustmentCommand(
        direction="credit",
        amount=Decimal("5.00"),
        reason_code="support_correction",
        comment="Ticket VPN-42 checked",
        expected_balance=Decimal("20.00"),
        expected_ledger_entry_id=opening_id,
    )
    request = _request(path=f"/api/admin/v1/users/{customer.id}/balance-adjustments")
    first = await service.adjust(
        request=request,
        principal=principal,
        user_id=customer.id,
        client_key="ticket-vpn-42",
        command=command,
    )
    replay = await service.adjust(
        request=request,
        principal=principal,
        user_id=customer.id,
        client_key="ticket-vpn-42",
        command=command,
    )

    assert first["applied"] is True and first["replayed"] is False
    assert replay["applied"] is False and replay["replayed"] is True
    assert first["ledger_entry_id"] == replay["ledger_entry_id"]
    assert first["previous_balance"] == "20.00"
    assert first["new_balance"] == replay["new_balance"] == "25.00"

    with pytest.raises(AdminIdempotencyConflict):
        await service.adjust(
            request=request,
            principal=principal,
            user_id=customer.id,
            client_key="ticket-vpn-42",
            command=BalanceAdjustmentCommand(
                direction="credit",
                amount=Decimal("5.00"),
                reason_code="different_reason",
                comment="Different request body",
            ),
        )
    with pytest.raises(AdminOptimisticConflict):
        await service.adjust(
            request=request,
            principal=principal,
            user_id=customer.id,
            client_key="ticket-vpn-43",
            command=BalanceAdjustmentCommand(
                direction="debit",
                amount=Decimal("1.00"),
                reason_code="support_correction",
                comment="Stale confirmation",
                expected_balance=Decimal("20.00"),
            ),
        )

    async with sessionmaker() as session:
        user = await session.get(User, customer.id)
        entries = (
            await session.scalars(
                select(LedgerEntry).where(
                    LedgerEntry.user_id == customer.id,
                    LedgerEntry.kind == LedgerKind.ADMIN_ADJUSTMENT.value,
                )
            )
        ).all()
        audits = (
            await session.scalars(
                select(AdminAuditEvent)
                .where(AdminAuditEvent.target_id == str(customer.id))
                .order_by(AdminAuditEvent.id)
            )
        ).all()
    assert user is not None and user.balance == Decimal("25.00")
    assert len(entries) == 1
    assert entries[0].details["reason_code"] == "support_correction"
    assert [event.action for event in audits] == [
        "balance.adjustment_applied",
        "balance.adjustment_replayed",
    ]
    assert audits[0].request_id == "admin-v1-test-request"
    assert audits[0].details["idempotency_key_hash"] != "ticket-vpn-42"


@pytest.mark.asyncio
async def test_balance_adjustment_rejects_losing_unique_race_with_another_body(
    monkeypatch,
):
    """The BillingRepo unique-key recovery must not weaken body idempotency."""

    customer = SimpleNamespace(id=73_101, balance=Decimal("20.00"))
    winner = SimpleNamespace(
        id=901,
        user_id=customer.id,
        amount=Decimal("5.00"),
        balance_after=Decimal("25.00"),
        details={"request_hash": "hash-from-the-concurrent-winner"},
    )

    class FakeSession:
        def __init__(self):
            self.scalar_calls = 0
            self.staged = []

        async def scalar(self, _statement):
            self.scalar_calls += 1
            return customer if self.scalar_calls == 1 else 0

        def add(self, value):
            self.staged.append(value)

    class FakeBilling:
        async def get_ledger_entry(self, _key):
            # Nothing was visible before the INSERT attempt.
            return None

        async def apply_balance_change(self, **_kwargs):
            # BillingRepo then lost the unique constraint race and returned
            # the concurrent winner with the same amount/kind.
            return SimpleNamespace(
                applied=False,
                ledger_entry=winner,
                user=customer,
            )

    session = FakeSession()
    repos = {
        "users": SimpleNamespace(session=session),
        "billing": FakeBilling(),
    }

    @asynccontextmanager
    async def fake_uow():
        yield repos

    monkeypatch.setattr("admin.services_v1.uow", fake_uow)
    principal = AdminPrincipal(
        user_id=1,
        username="owner",
        role=AdminRole.OWNER,
        session_id=1,
        csrf_token_hash="unused",
        expires_at=NOW + timedelta(hours=1),
    )
    service = AdminBalanceService()

    with pytest.raises(AdminIdempotencyConflict, match="another request"):
        await service.adjust(
            request=_request(),
            principal=principal,
            user_id=customer.id,
            client_key="concurrent-admin-key",
            command=BalanceAdjustmentCommand(
                direction="credit",
                amount=Decimal("5.00"),
                reason_code="losing_request",
                comment="Same amount but different business reason",
            ),
        )
    assert session.staged == []


@pytest.mark.asyncio
async def test_rejected_balance_adjustment_is_durably_audited(sessionmaker):
    principal, csrf_token = await _admin_principal(sessionmaker)
    customer = await UserService(uow).register(
        73_102,
        username="rejected-balance-user",
        balance="20.00",
    )
    app = _test_app(principal, users_router)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://admin.test",
        headers={"Origin": "https://admin.test"},
    ) as client:
        client.cookies.set(CSRF_COOKIE_NAME, csrf_token, path=CSRF_COOKIE_PATH)
        response = await client.post(
            f"/api/admin/v1/users/{customer.id}/balance-adjustments",
            headers={
                "X-CSRF-Token": csrf_token,
                "Idempotency-Key": "rejected-balance-key",
            },
            json={
                "direction": "debit",
                "amount": "1.00",
                "reason_code": "support_correction",
                "comment": "Stale operator confirmation",
                "expected_balance": "19.00",
            },
        )

    assert response.status_code == 409
    async with sessionmaker() as session:
        persisted_user = await session.get(User, customer.id)
        event = await session.scalar(
            select(AdminAuditEvent).where(
                AdminAuditEvent.action == "balance.adjustment_rejected"
            )
        )
    assert persisted_user is not None and persisted_user.balance == Decimal("20.00")
    assert event is not None
    assert event.actor_user_id == principal.user_id
    assert event.target_id == str(customer.id)
    assert event.details["outcome"] == "rejected"
    assert event.details["error_code"] == "AdminOptimisticConflict"
    assert event.details["idempotency_key_hash"] != "rejected-balance-key"


@pytest.mark.asyncio
async def test_analytics_definitions_and_timezone_bucket_boundaries(sessionmaker):
    bucket_time = datetime(2026, 7, 13, 20, 30, tzinfo=timezone.utc)
    period_from = datetime(2026, 7, 1, tzinfo=timezone.utc)
    period_to = datetime(2026, 7, 31, tzinfo=timezone.utc)
    async with sessionmaker() as session, session.begin():
        user = User(
            tg_id=74_001,
            username="analytics-user",
            balance=Decimal("77.00"),
            created=bucket_time,
        )
        session.add(user)
        await session.flush()
        server = Server(
            name="analytics-server",
            ip="192.0.2.20",
            port=443,
            host="analytics.example.test",
            location="FI",
            api_key="manager-key",
            monthly_cost=Decimal("30.00"),
        )
        session.add(server)
        session.add(
            ProviderPayment(
                intent_id="analytics-payment",
                user_id=user.id,
                provider="telegram",
                provider_payment_id="analytics-charge",
                amount=Decimal("100.00"),
                currency="RUB",
                payload="topup:analytics-payment",
                status="credited",
                created_at=bucket_time,
                expires_at=bucket_time + timedelta(hours=1),
                credited_at=bucket_time,
            )
        )
        movements = (
            ("periodic", "-10.00", LedgerKind.PERIODIC_CHARGE),
            ("reservation", "-5.00", LedgerKind.CONFIG_RESERVATION),
            ("refund", "2.00", LedgerKind.CONFIG_REFUND),
            ("referral", "5.00", LedgerKind.REFERRAL_REWARD_L1),
            ("manual", "3.00", LedgerKind.ADMIN_ADJUSTMENT),
            ("opening", "20.00", LedgerKind.OPENING_BALANCE),
        )
        running_balance = Decimal("100.00")
        for key, amount, kind in movements:
            value = Decimal(amount)
            running_balance += value
            session.add(
                LedgerEntry(
                    user_id=user.id,
                    amount=value,
                    balance_after=running_balance,
                    kind=kind.value,
                    idempotency_key=f"analytics:{key}",
                    created_at=bucket_time,
                )
            )

    analytics = AdminAnalyticsQueryService(uow)
    overview = await analytics.overview(
        period_from=period_from,
        period_to=period_to,
    )
    assert overview["users"]["total"] == 1
    assert overview["users"]["new"] == 1
    assert overview["users"]["paying"] == 1
    assert overview["finance"] == {
        "cash_in": "100.00",
        "service_charges": "10.00",
        "config_fees": "5.00",
        "config_refunds": "2.00",
        "recognized_revenue": "13.00",
        "referral_rewards": "5.00",
        "manual_adjustments": "3.00",
        "opening_balances": "20.00",
        "wallet_liability": "77.00",
        "wallet_debt": "0.00",
        "infrastructure_monthly_run_rate": "30.00",
        "allocated_infrastructure_cost": "30.00",
        "estimated_margin": "-22.00",
    }

    series = await analytics.finance_timeseries(
        period_from=period_from,
        period_to=period_to,
        granularity="day",
        timezone_name="Asia/Novosibirsk",
    )
    assert series == [
        {
            "bucket": "2026-07-14",
            "cash_in": "100.00",
            "service_charges": "10.00",
            "config_fees": "5.00",
            "config_refunds": "2.00",
            "recognized_revenue": "13.00",
            "referral_rewards": "5.00",
            "manual_adjustments": "3.00",
        }
    ]
    with pytest.raises(ValueError, match="Unknown timezone"):
        await analytics.finance_timeseries(
            period_from=period_from,
            period_to=period_to,
            timezone_name="Not/A_Timezone",
        )


@pytest.mark.asyncio
async def test_config_action_api_is_csrf_protected_and_audited(
    monkeypatch,
    sessionmaker,
):
    principal, csrf_token = await _admin_principal(sessionmaker)

    class FakeConfigService:
        async def suspend_config(self, config_id: int):
            assert config_id == 777
            return SimpleNamespace(
                desired_state="suspended",
                actual_state="suspended",
                operation_id="op-777",
                last_error=None,
            )

        async def unsuspend_config(self, config_id: int):  # pragma: no cover
            raise AssertionError(config_id)

        async def revoke_config(self, config_id: int):  # pragma: no cover
            raise AssertionError(config_id)

    monkeypatch.setattr(admin_v1_configs, "config_service", FakeConfigService())
    app = _test_app(principal, configs_router)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://admin.test",
        headers={"Origin": "https://admin.test"},
    ) as client:
        client.cookies.set(CSRF_COOKIE_NAME, csrf_token, path=CSRF_COOKIE_PATH)
        missing_csrf = await client.post(
            "/api/admin/v1/configs/777/actions",
            json={"action": "suspend", "reason": "Support ticket VPN-77"},
        )
        assert missing_csrf.status_code == 403
        completed = await client.post(
            "/api/admin/v1/configs/777/actions",
            headers={"X-CSRF-Token": csrf_token},
            json={"action": "suspend", "reason": "Support ticket VPN-77"},
        )

    assert completed.status_code == 200
    assert completed.json() == {
        "config_id": 777,
        "action": "suspend",
        "state": "completed",
        "config": {
            "desired_state": "suspended",
            "actual_state": "suspended",
            "operation_id": "op-777",
            "last_error": None,
        },
    }
    async with sessionmaker() as session:
        event = await session.scalar(
            select(AdminAuditEvent).where(AdminAuditEvent.action == "config.suspend")
        )
    assert event is not None
    assert event.actor_user_id == principal.user_id
    assert event.target_id == "777"
    assert event.details == {
        "reason": "Support ticket VPN-77",
        "outcome": "completed",
        "error": None,
    }


@pytest.mark.asyncio
async def test_audit_api_recursively_redacts_secrets(sessionmaker):
    principal, _ = await _admin_principal(sessionmaker)
    async with sessionmaker() as session, session.begin():
        session.add(
            AdminAuditEvent(
                actor_user_id=principal.user_id,
                action="security.redaction_test",
                target_type="system",
                target_id="redaction",
                request_id="redaction-request",
                correlation_id="redaction-correlation",
                details={
                    "password": "hunter2",
                    "Authorization": "Bearer session-secret",
                    "Set-Cookie": "session=secret",
                    "safe": "visible",
                    "nested": {
                        "api_key": "manager-secret",
                        "rows": [
                            {
                                "token": "telegram-token",
                                "bot_token": "telegram-bot-token",
                                "service_credential_id": "credential-secret",
                                "count": 2,
                            }
                        ],
                    },
                },
            )
        )

    app = _test_app(principal, system_router)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://admin.test",
    ) as client:
        response = await client.get(
            "/api/admin/v1/audit-events",
            params={"action": "security.redaction_test"},
        )

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["details"] == {
        "password": "[redacted]",
        "Authorization": "[redacted]",
        "Set-Cookie": "[redacted]",
        "safe": "visible",
        "nested": {
            "api_key": "[redacted]",
            "rows": [
                {
                    "token": "[redacted]",
                    "bot_token": "[redacted]",
                    "service_credential_id": "[redacted]",
                    "count": 2,
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_observability_summary_reports_degraded_dependencies_and_queues(
    monkeypatch,
    sessionmaker,
):
    principal, _ = await _admin_principal(sessionmaker)
    expiry = NOW + timedelta(days=20)

    async def readiness():
        return {"database": True, "redis": False, "manager_tls": True}

    monkeypatch.setattr(admin_v1_system, "dependency_readiness", readiness)
    monkeypatch.setattr(
        admin_v1_system,
        "inspect_manager_tls_material",
        lambda: ManagerTLSStatus(
            enabled=True,
            ready=True,
            certificate_not_after=(("client", expiry),),
        ),
    )

    async with sessionmaker() as session, session.begin():
        session.add_all(
            [
                VPNOperation(
                    operation_id="observability-operation",
                    config_name="cfg-observability",
                    kind="suspend",
                    payload={},
                    status="failed",
                    attempts=3,
                    next_attempt_at=NOW,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                NotificationOutbox(
                    dedupe_key="observability-notification",
                    chat_id=74_002,
                    text="queued",
                    status="pending",
                    next_attempt_at=NOW,
                ),
                TelegramUpdateInbox(
                    update_id=74_003,
                    payload={"update_id": 74_003},
                    source="polling",
                    ordering_key="ordering-key",
                    status="dead",
                    next_attempt_at=NOW,
                ),
                BillingRun(
                    period_key="observability-period",
                    period_start=NOW - timedelta(days=1),
                    period_end=NOW,
                    cost_per_config=Decimal("1.67"),
                    status="completed",
                    charged_users=8,
                    total_amount=Decimal("13.36"),
                    completed_at=NOW,
                ),
            ]
        )

    payload = await admin_v1_system.observability_summary(principal)
    assert payload["status"] == "degraded"
    assert payload["dependencies"] == {
        "database": True,
        "redis": False,
        "manager_tls": True,
    }
    assert payload["manager_tls"] == {
        "enabled": True,
        "ready": True,
        "certificate_expiry": {"client": expiry.isoformat()},
    }
    assert payload["vpn_operations"] == {"failed": 1}
    assert payload["notification_outbox"] == {"pending": 1}
    assert payload["telegram_inbox"] == {"dead": 1}
    assert payload["billing"]["period_key"] == "observability-period"
    assert payload["billing"]["total_amount"] == "13.36"
    assert payload["alerts"] == []
