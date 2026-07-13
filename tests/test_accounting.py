from decimal import Decimal

import pytest

from core.db.models import LedgerKind
from core.db.unit_of_work import uow
from core.exceptions import InvalidOperationError, UserNotFoundError
from core.services import AccountingService, BillingService, UserService


@pytest.mark.asyncio
async def test_balance_history_is_private_newest_first_and_paginated(sessionmaker):
    users = UserService(uow)
    billing = BillingService(uow, per_config_cost="1.00")
    accounting = AccountingService(uow)
    alice = await users.register(91_001, balance="10.00")
    bob = await users.register(91_002, balance="20.00")

    await billing.top_up(alice.id, "5.00", idempotency_key="alice:manual:1")
    await billing.withdraw(alice.id, "2.00", idempotency_key="alice:manual:2")

    first_page = await accounting.list_balance_history(alice.id, limit=2)
    await billing.top_up(alice.id, "1.00", idempotency_key="alice:later:3")
    second_page = await accounting.list_balance_history(
        alice.id,
        limit=2,
        offset=2,
        snapshot_id=first_page.snapshot_id,
    )

    assert first_page.total == 3
    assert first_page.offset == 0
    assert [item.amount for item in first_page.items] == [
        Decimal("-2.00"),
        Decimal("5.00"),
    ]
    assert [item.kind for item in first_page.items] == [
        LedgerKind.MANUAL_WITHDRAWAL.value,
        LedgerKind.MANUAL_TOP_UP.value,
    ]
    assert [item.balance_after for item in first_page.items] == [
        Decimal("13.00"),
        Decimal("15.00"),
    ]
    assert [item.amount for item in second_page.items] == [Decimal("10.00")]
    assert all(item.id for item in first_page.items + second_page.items)

    refreshed = await accounting.list_balance_history(alice.id, limit=2)
    assert refreshed.total == 4
    assert refreshed.snapshot_id > first_page.snapshot_id
    assert refreshed.items[0].amount == Decimal("1.00")

    bob_page = await accounting.list_balance_history(bob.id)
    assert bob_page.total == 1
    assert [item.amount for item in bob_page.items] == [Decimal("20.00")]


@pytest.mark.asyncio
async def test_balance_history_validates_owner_and_pagination(sessionmaker):
    accounting = AccountingService(uow)

    with pytest.raises(UserNotFoundError):
        await accounting.list_balance_history(999_999)
    with pytest.raises(InvalidOperationError):
        await accounting.list_balance_history(1, limit=0)
    with pytest.raises(InvalidOperationError):
        await accounting.list_balance_history(1, limit=51)
    with pytest.raises(InvalidOperationError):
        await accounting.list_balance_history(1, offset=-1)
    with pytest.raises(InvalidOperationError):
        await accounting.list_balance_history(1, offset=1_000_001)
    with pytest.raises(InvalidOperationError):
        await accounting.list_balance_history(1, direction="all")


@pytest.mark.asyncio
async def test_balance_history_filters_credits_and_debits_with_stable_snapshot(
    sessionmaker,
):
    users = UserService(uow)
    billing = BillingService(uow, per_config_cost="1.00")
    accounting = AccountingService(uow)
    user = await users.register(91_003, balance="10.00")

    await billing.top_up(user.id, "5.00", idempotency_key="filter:credit:1")
    await billing.withdraw(user.id, "2.00", idempotency_key="filter:debit:1")

    credits = await accounting.list_balance_history(
        user.id,
        direction="credit",
        limit=1,
    )
    debits = await accounting.list_balance_history(user.id, direction="debit")

    assert credits.total == 2
    assert [item.amount for item in credits.items] == [Decimal("5.00")]
    assert debits.total == 1
    assert [item.amount for item in debits.items] == [Decimal("-2.00")]

    await billing.top_up(user.id, "3.00", idempotency_key="filter:credit:2")
    stable_second_page = await accounting.list_balance_history(
        user.id,
        direction="credit",
        limit=1,
        offset=1,
        snapshot_id=credits.snapshot_id,
    )
    refreshed = await accounting.list_balance_history(user.id, direction="credit")

    assert stable_second_page.total == 2
    assert [item.amount for item in stable_second_page.items] == [Decimal("10.00")]
    assert refreshed.total == 3
    assert [item.amount for item in refreshed.items] == [
        Decimal("3.00"),
        Decimal("5.00"),
        Decimal("10.00"),
    ]
