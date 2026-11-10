from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from core.config import settings
from core.db.models import LedgerEntry, LedgerKind, ProviderPayment, User
from core.db.models.subscription import Subscription
from core.db.unit_of_work import uow
from core.services.admin_queries import AdminAnalyticsQueryService

EVENT_TIME = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
PERIOD = {
    "period_from": EVENT_TIME - timedelta(days=1),
    "period_to": EVENT_TIME + timedelta(days=1),
}


async def _seed_accounts(sessionmaker):
    accounts = {}
    definitions = (
        ("paid", "basic", "active", False, False, "10.00"),
        ("manual", "plus", "active", True, False, "20.00"),
        ("configured", "basic", "active", False, False, "30.00"),
        ("unlimited", "plus", "active", False, True, "40.00"),
        ("both", "plus", "active", True, False, "50.00"),
        ("suspended", "plus", "suspended", False, False, "-6.00"),
        ("pending", "basic", "pending", False, False, "7.00"),
    )
    async with sessionmaker() as session, session.begin():
        for index, (name, plan, status, excluded, unlimited, balance) in enumerate(
            definitions, start=1
        ):
            user = User(
                tg_id=9_050_000 + index,
                username=f"analytics-{name}",
                balance=Decimal(balance),
                exclude_from_business_metrics=excluded,
                unlimited_balance=unlimited,
                created=EVENT_TIME.replace(tzinfo=None),
            )
            session.add(user)
            await session.flush()
            accounts[name] = user.id
            session.add(Subscription(user_id=user.id, plan_code=plan, status=status))
            session.add(
                ProviderPayment(
                    intent_id=f"product-analytics-{name}",
                    user_id=user.id,
                    provider="telegram",
                    provider_payment_id=f"product-analytics-charge-{name}",
                    amount=Decimal(index * 100),
                    currency="RUB",
                    payload=f"topup:product-analytics-{name}",
                    status="credited",
                    created_at=EVENT_TIME,
                    expires_at=EVENT_TIME + timedelta(hours=1),
                    credited_at=EVENT_TIME,
                )
            )
            session.add(
                LedgerEntry(
                    user_id=user.id,
                    amount=-Decimal(index),
                    balance_after=Decimal(balance),
                    kind=LedgerKind.PERIODIC_CHARGE.value,
                    idempotency_key=f"product-analytics:{name}",
                    created_at=EVENT_TIME,
                )
            )
    return accounts


async def _financial_snapshot(sessionmaker):
    async with sessionmaker() as session:
        wallets = list(
            await session.execute(select(User.id, User.balance).order_by(User.id))
        )
        ledger = list(
            await session.execute(
                select(
                    LedgerEntry.id,
                    LedgerEntry.user_id,
                    LedgerEntry.amount,
                    LedgerEntry.balance_after,
                    LedgerEntry.kind,
                    LedgerEntry.idempotency_key,
                ).order_by(LedgerEntry.id)
            )
        )
        payments = list(
            await session.execute(
                select(
                    ProviderPayment.id,
                    ProviderPayment.user_id,
                    ProviderPayment.amount,
                    ProviderPayment.status,
                ).order_by(ProviderPayment.id)
            )
        )
    return wallets, ledger, payments


@pytest.mark.asyncio
async def test_manual_and_configured_business_exclusions_are_unioned_not_applied_to_all(
    sessionmaker, monkeypatch
):
    accounts = await _seed_accounts(sessionmaker)
    monkeypatch.setattr(settings, "billing_enabled", False)
    monkeypatch.setattr(
        settings,
        "business_metrics_excluded_user_ids",
        f"{accounts['configured']},{accounts['both']}",
    )
    before = await _financial_snapshot(sessionmaker)
    business = AdminAnalyticsQueryService(uow)
    all_accounts = AdminAnalyticsQueryService(uow, scope="all")

    overview = await business.overview(**PERIOD)
    total_overview = await all_accounts.overview(**PERIOD)
    series = await business.finance_timeseries(**PERIOD)
    total_series = await all_accounts.finance_timeseries(**PERIOD)

    # Capacity/account counts remain operational truth for every scope.
    assert overview["users"]["total"] == total_overview["users"]["total"] == 7
    assert overview["users"]["delivery"] == {"active": 7}
    assert overview["users"]["new"] == overview["users"]["paying"] == 4
    assert total_overview["users"]["new"] == total_overview["users"]["paying"] == 7
    assert overview["finance"]["cash_in"] == "1800.00"
    assert overview["finance"]["service_charges"] == "18.00"
    assert overview["finance"]["wallet_liability"] == "57.00"
    assert overview["finance"]["wallet_debt"] == "6.00"
    assert total_overview["finance"]["cash_in"] == "2800.00"
    assert total_overview["finance"]["service_charges"] == "28.00"
    assert total_overview["finance"]["wallet_liability"] == "157.00"
    assert series[0]["cash_in"] == "1800.00"
    assert series[0]["service_charges"] == "18.00"
    assert total_series[0]["cash_in"] == "2800.00"

    # One paid account is one hourly unit even with no credential rows yet;
    # suspended and pending subscriptions do not forecast a charge.
    assert overview["subscription_forecast"] == {
        "scope": "business",
        "billing_paused": True,
        "active_accounts": 2,
        "paid_active_accounts": 1,
        "unlimited_active_accounts": 1,
        "hourly_rate_rub": "0.07",
        "monthly_estimate_rub": "50.40",
        "monthly_plan_total_rub": "50.00",
        "currency": "RUB",
    }
    assert total_overview["subscription_forecast"] == {
        "scope": "all",
        "billing_paused": True,
        "active_accounts": 5,
        "paid_active_accounts": 4,
        "unlimited_active_accounts": 1,
        "hourly_rate_rub": "0.42",
        "monthly_estimate_rub": "302.40",
        "monthly_plan_total_rub": "300.00",
        "currency": "RUB",
    }
    assert await _financial_snapshot(sessionmaker) == before


@pytest.mark.asyncio
async def test_unlimited_changes_only_paid_forecast_and_manual_exclusion_updates_live(
    sessionmaker, monkeypatch
):
    accounts = await _seed_accounts(sessionmaker)
    monkeypatch.setattr(settings, "billing_enabled", False)
    monkeypatch.setattr(
        settings,
        "business_metrics_excluded_user_ids",
        f"{accounts['configured']},{accounts['both']}",
    )
    analytics = AdminAnalyticsQueryService(uow)
    before = await analytics.overview(**PERIOD)
    ledger_before = await _financial_snapshot(sessionmaker)
    async with sessionmaker() as session, session.begin():
        user = await session.get(User, accounts["unlimited"])
        user.unlimited_balance = False

    after_unlimited = await analytics.overview(**PERIOD)
    assert after_unlimited["finance"] == before["finance"]
    assert after_unlimited["users"]["paying"] == before["users"]["paying"]
    assert after_unlimited["subscription_forecast"]["paid_active_accounts"] == 2
    assert after_unlimited["subscription_forecast"]["unlimited_active_accounts"] == 0
    assert after_unlimited["subscription_forecast"]["hourly_rate_rub"] == "0.21"
    assert after_unlimited["subscription_forecast"]["monthly_estimate_rub"] == "151.20"
    assert (
        after_unlimited["subscription_forecast"]["monthly_plan_total_rub"] == "150.00"
    )

    # The same service instance sees flag changes without an API restart. An
    # overlap remains excluded until both independent rules are removed.
    async with sessionmaker() as session, session.begin():
        (await session.get(User, accounts["manual"])).exclude_from_business_metrics = (
            False
        )
        (await session.get(User, accounts["both"])).exclude_from_business_metrics = (
            False
        )
    after_manual = await analytics.overview(**PERIOD)
    assert after_manual["users"]["new"] == 5
    assert after_manual["finance"]["cash_in"] == "2000.00"
    assert after_manual["finance"]["wallet_liability"] == "77.00"
    assert after_manual["subscription_forecast"]["paid_active_accounts"] == 3
    assert after_manual["subscription_forecast"]["hourly_rate_rub"] == "0.35"
    assert after_manual["subscription_forecast"]["monthly_plan_total_rub"] == "250.00"
    assert await _financial_snapshot(sessionmaker) == ledger_before


def test_unknown_analytics_scope_fails_closed():
    with pytest.raises(ValueError, match="Unknown analytics scope"):
        AdminAnalyticsQueryService(uow, scope="invalid")
