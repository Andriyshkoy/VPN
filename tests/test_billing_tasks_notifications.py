from datetime import datetime, timezone
from decimal import Decimal

import pytest

import billing_daemon.billing_tasks as billing_tasks
from core.services.models import BillingSettings, User


def _make_user(user_id: int, balance: Decimal) -> User:
    return User(
        id=user_id,
        tg_id=user_id,
        username=f"u{user_id}",
        created=datetime.now(timezone.utc).replace(tzinfo=None),
        balance=balance,
    )


def _make_settings(monthly_cost: Decimal) -> BillingSettings:
    return BillingSettings(
        id=1,
        config_creation_cost=Decimal("0"),
        monthly_config_cost=monthly_cost,
        referral_first_deposit_bonus_pct=Decimal("0"),
        referral_recurring_bonus_pct=Decimal("0"),
        updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )


@pytest.mark.asyncio
async def test_billing_tasks_send_pause_message(monkeypatch):
    sent = []

    class DummyBillingService:
        def __init__(self, _uow):
            pass

        async def charge_usage(self):
            return {_make_user(1, Decimal("0")): Decimal("1.00")}

        async def get_settings(self):
            return _make_settings(Decimal("720"))

    class DummyConfigService:
        def __init__(self, _uow):
            pass

        async def count_active(self, user_id):
            return 1

    class DummyNotificationService:
        def __init__(self, *a, **kw):
            pass

        async def enqueue(self, chat_id, text):
            sent.append((chat_id, text))

    monkeypatch.setattr(billing_tasks, "BillingService", DummyBillingService)
    monkeypatch.setattr(billing_tasks, "ConfigService", DummyConfigService)
    monkeypatch.setattr(billing_tasks, "NotificationService", DummyNotificationService)

    await billing_tasks._charge_all_and_notify_async()

    assert sent and "\u043f\u043e\u0441\u0442\u0430\u0432\u043b\u0435\u043d \u043d\u0430 \u043f\u0430\u0443\u0437\u0443" in sent[0][1]


@pytest.mark.asyncio
async def test_billing_tasks_send_day_warning(monkeypatch):
    sent = []

    class DummyBillingService:
        def __init__(self, _uow):
            pass

        async def charge_usage(self):
            return {_make_user(2, Decimal("20")): Decimal("1.00")}

        async def get_settings(self):
            return _make_settings(Decimal("720"))

    class DummyConfigService:
        def __init__(self, _uow):
            pass

        async def count_active(self, user_id):
            return 1

    class DummyNotificationService:
        def __init__(self, *a, **kw):
            pass

        async def enqueue(self, chat_id, text):
            sent.append((chat_id, text))

    monkeypatch.setattr(billing_tasks, "BillingService", DummyBillingService)
    monkeypatch.setattr(billing_tasks, "ConfigService", DummyConfigService)
    monkeypatch.setattr(billing_tasks, "NotificationService", DummyNotificationService)

    await billing_tasks._charge_all_and_notify_async()

    assert sent and "\u0441\u0443\u0442\u043a\u0438" in sent[0][1]


@pytest.mark.asyncio
async def test_billing_tasks_send_week_warning(monkeypatch):
    sent = []

    class DummyBillingService:
        def __init__(self, _uow):
            pass

        async def charge_usage(self):
            return {_make_user(3, Decimal("100")): Decimal("1.00")}

        async def get_settings(self):
            return _make_settings(Decimal("720"))

    class DummyConfigService:
        def __init__(self, _uow):
            pass

        async def count_active(self, user_id):
            return 1

    class DummyNotificationService:
        def __init__(self, *a, **kw):
            pass

        async def enqueue(self, chat_id, text):
            sent.append((chat_id, text))

    monkeypatch.setattr(billing_tasks, "BillingService", DummyBillingService)
    monkeypatch.setattr(billing_tasks, "ConfigService", DummyConfigService)
    monkeypatch.setattr(billing_tasks, "NotificationService", DummyNotificationService)

    await billing_tasks._charge_all_and_notify_async()

    assert sent and "\u043d\u0435\u0434\u0435\u043b\u044e" in sent[0][1]


@pytest.mark.asyncio
async def test_billing_tasks_skip_when_no_active_configs(monkeypatch):
    sent = []

    class DummyBillingService:
        def __init__(self, _uow):
            pass

        async def charge_usage(self):
            return {_make_user(4, Decimal("20")): Decimal("1.00")}

        async def get_settings(self):
            return _make_settings(Decimal("720"))

    class DummyConfigService:
        def __init__(self, _uow):
            pass

        async def count_active(self, user_id):
            return 0

    class DummyNotificationService:
        def __init__(self, *a, **kw):
            pass

        async def enqueue(self, chat_id, text):
            sent.append((chat_id, text))

    monkeypatch.setattr(billing_tasks, "BillingService", DummyBillingService)
    monkeypatch.setattr(billing_tasks, "ConfigService", DummyConfigService)
    monkeypatch.setattr(billing_tasks, "NotificationService", DummyNotificationService)

    await billing_tasks._charge_all_and_notify_async()

    assert sent == []
