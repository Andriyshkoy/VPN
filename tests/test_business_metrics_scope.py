from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from core.config import Settings, settings
from core.db.models import (
    BillingRun,
    LedgerEntry,
    LedgerKind,
    ProviderPayment,
    ReferralReward,
    Server,
    User,
    VPN_Config,
)
from core.db.unit_of_work import uow
from core.observability.snapshot import render_prometheus_metrics
from core.services.admin_queries import (
    AdminAnalyticsQueryService,
    AdminUserQueryService,
)


def _settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        database_url=settings.database_url,
        encryption_key=settings.encryption_key,
        **overrides,
    )


def test_business_metric_exclusion_ids_are_canonical_and_fail_closed():
    configured = _settings(
        business_metrics_excluded_user_ids=" 42,7,42 ",
    )
    assert configured.business_metrics_excluded_user_ids == "42,7"
    assert configured.business_metrics_excluded_ids == (42, 7)

    for invalid in ("0", "-1", "owner", "2147483648"):
        with pytest.raises(ValidationError):
            _settings(business_metrics_excluded_user_ids=invalid)


@pytest.mark.asyncio
async def test_owner_is_excluded_only_from_aggregate_business_reporting(
    sessionmaker,
    monkeypatch,
):
    period_from = datetime(2026, 7, 1, tzinfo=timezone.utc)
    period_to = datetime(2026, 8, 1, tzinfo=timezone.utc)
    event_time = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)

    async with sessionmaker() as session, session.begin():
        customer = User(
            tg_id=880_001,
            username="real-customer",
            balance=Decimal("144.00"),
            created=event_time,
        )
        owner = User(
            tg_id=880_002,
            username="owner-test-account",
            balance=Decimal("3000004.00"),
            created=event_time,
        )
        session.add_all([customer, owner])
        await session.flush()
        # The included customer remains a real acquisition and liability even
        # when their inviter is an internal account.
        customer.referred_by_id = owner.id

        server = Server(
            name="business-scope-server",
            ip="192.0.2.88",
            port=443,
            host="business-scope.example.test",
            location="FI",
            api_key="manager-key",
        )
        session.add(server)
        await session.flush()
        session.add_all(
            [
                VPN_Config(
                    name="customer-config",
                    display_name="Customer config",
                    owner_id=customer.id,
                    server_id=server.id,
                    desired_state="active",
                    actual_state="active",
                ),
                VPN_Config(
                    name="owner-config",
                    display_name="Owner config",
                    owner_id=owner.id,
                    server_id=server.id,
                    desired_state="active",
                    actual_state="active",
                ),
            ]
        )

        run = BillingRun(
            period_key="business-scope-period",
            period_start=event_time - timedelta(hours=1),
            period_end=event_time,
            cost_per_config=Decimal("1.00"),
            status="completed",
            charged_users=2,
            total_amount=Decimal("2.00"),
            completed_at=event_time,
        )
        session.add(run)
        await session.flush()

        customer_payment = ProviderPayment(
            intent_id="customer-payment",
            user_id=customer.id,
            provider="telegram",
            provider_payment_id="customer-charge",
            amount=Decimal("100.00"),
            currency="RUB",
            payload="topup:customer-payment",
            status="credited",
            created_at=event_time,
            expires_at=event_time + timedelta(hours=1),
            credited_at=event_time,
        )
        owner_payment = ProviderPayment(
            intent_id="owner-payment",
            user_id=owner.id,
            provider="telegram",
            provider_payment_id="owner-charge",
            amount=Decimal("900.00"),
            currency="RUB",
            payload="topup:owner-payment",
            status="credited",
            created_at=event_time,
            expires_at=event_time + timedelta(hours=1),
            credited_at=event_time,
        )
        session.add_all(
            [
                customer_payment,
                owner_payment,
                LedgerEntry(
                    user_id=customer.id,
                    amount=Decimal("100.00"),
                    balance_after=Decimal("100.00"),
                    kind=LedgerKind.OPENING_BALANCE.value,
                    idempotency_key="business-scope:customer:opening",
                    created_at=event_time,
                ),
                LedgerEntry(
                    user_id=owner.id,
                    amount=Decimal("3000000.00"),
                    balance_after=Decimal("3000000.00"),
                    kind=LedgerKind.OPENING_BALANCE.value,
                    idempotency_key="business-scope:owner:opening",
                    created_at=event_time,
                ),
                LedgerEntry(
                    user_id=customer.id,
                    amount=Decimal("-1.00"),
                    balance_after=Decimal("99.00"),
                    kind=LedgerKind.PERIODIC_CHARGE.value,
                    idempotency_key="business-scope:customer:billing",
                    reference_type="billing_run",
                    reference_id=str(run.id),
                    created_at=event_time,
                ),
                LedgerEntry(
                    user_id=owner.id,
                    amount=Decimal("-1.00"),
                    balance_after=Decimal("2999999.00"),
                    kind=LedgerKind.PERIODIC_CHARGE.value,
                    idempotency_key="business-scope:owner:billing",
                    reference_type="billing_run",
                    reference_id=str(run.id),
                    created_at=event_time,
                ),
            ]
        )
        await session.flush()

        reward_from_owner = LedgerEntry(
            user_id=customer.id,
            amount=Decimal("45.00"),
            balance_after=Decimal("144.00"),
            kind=LedgerKind.REFERRAL_REWARD_L1.value,
            idempotency_key="business-scope:reward-from-owner",
            reference_type="referral_reward",
            reference_id=f"payment:{owner_payment.id}:level:1",
            created_at=event_time,
        )
        reward_to_owner = LedgerEntry(
            user_id=owner.id,
            amount=Decimal("5.00"),
            balance_after=Decimal("3000004.00"),
            kind=LedgerKind.REFERRAL_REWARD_L1.value,
            idempotency_key="business-scope:reward-to-owner",
            reference_type="referral_reward",
            reference_id=f"payment:{customer_payment.id}:level:1",
            created_at=event_time,
        )
        session.add_all([reward_from_owner, reward_to_owner])
        await session.flush()
        session.add_all(
            [
                ReferralReward(
                    source_payment_id=owner_payment.id,
                    source_user_id=owner.id,
                    beneficiary_user_id=customer.id,
                    level=1,
                    rate_bps=500,
                    source_amount=Decimal("900.00"),
                    reward_amount=Decimal("45.00"),
                    currency="RUB",
                    ledger_entry_id=reward_from_owner.id,
                    program_version="v1-5pct-1pct",
                    created_at=event_time,
                ),
                ReferralReward(
                    source_payment_id=customer_payment.id,
                    source_user_id=customer.id,
                    beneficiary_user_id=owner.id,
                    level=1,
                    rate_bps=500,
                    source_amount=Decimal("100.00"),
                    reward_amount=Decimal("5.00"),
                    currency="RUB",
                    ledger_entry_id=reward_to_owner.id,
                    program_version="v1-5pct-1pct",
                    created_at=event_time,
                ),
            ]
        )

    monkeypatch.setattr(
        settings,
        "business_metrics_excluded_user_ids",
        str(owner.id),
    )
    # Exercise the same settings-driven construction used by the API singleton.
    analytics = AdminAnalyticsQueryService(uow)
    dashboard = await analytics.dashboard(
        period_from=period_from,
        period_to=period_to,
    )
    # Operational population/configuration counts stay complete.
    assert dashboard["users"]["total"] == 2
    assert dashboard["users"]["new"] == 1
    assert dashboard["users"]["with_configs"] == 2
    assert dashboard["users"]["paying"] == 1
    assert dashboard["users"]["invited"] == 1
    assert dashboard["configs"] == {"active": 2}
    assert dashboard["finance"]["cash_in"] == "100.00"
    assert dashboard["finance"]["opening_balances"] == "100.00"
    assert dashboard["finance"]["wallet_liability"] == "144.00"
    assert dashboard["finance"]["referral_rewards"] == "45.00"
    # Billing-run observability remains the persisted accounting truth.
    assert dashboard["billing"]["charged_users"] == 2
    assert dashboard["billing"]["total_amount"] == "2.00"

    # Server/fleet capacity remains operational truth rather than a filtered
    # business KPI.
    assert dashboard["servers"][0]["config_total"] == 2

    prometheus = await render_prometheus_metrics(
        redis_is_ready=False,
        now=event_time + timedelta(minutes=1),
    )
    assert "vpn_hub_billing_last_charged_users 2" in prometheus
    assert "vpn_hub_billing_last_amount_rubles 2.00" in prometheus

    timeseries = await analytics.finance_timeseries(
        period_from=period_from,
        period_to=period_to,
    )
    assert timeseries == [
        {
            "bucket": "2026-07-20",
            "cash_in": "100.00",
            "service_charges": "1.00",
            "config_fees": "0.00",
            "config_refunds": "0.00",
            "recognized_revenue": "1.00",
            "referral_rewards": "45.00",
            "manual_adjustments": "0.00",
        }
    ]

    # Management and accounting sources remain complete and addressable.
    users = await AdminUserQueryService(uow).list_users(limit=100)
    assert users["total"] == 2
    assert {item["id"] for item in users["items"]} == {customer.id, owner.id}
    async with sessionmaker() as session:
        assert (
            await session.scalar(select(func.count()).select_from(LedgerEntry))
        ) == 6
