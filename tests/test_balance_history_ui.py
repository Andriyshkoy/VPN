from datetime import datetime, timezone
from decimal import Decimal

from bot.handlers.balance_history import render_balance_history
from bot.keyboards import balance_history_keyboard
from core.services import BalanceHistoryItem, BalanceHistoryPage


def _item(*, item_id: int, amount: str, kind: str, balance_after: str):
    return BalanceHistoryItem(
        id=item_id,
        amount=Decimal(amount),
        balance_after=Decimal(balance_after),
        kind=kind,
        reference_type=None,
        reference_id=None,
        details={"private": "must-not-be-rendered"},
        created_at=datetime(2026, 7, 13, 10, item_id, tzinfo=timezone.utc),
    )


def test_balance_history_renders_referral_and_charge_details_without_metadata():
    page = BalanceHistoryPage(
        items=(
            _item(
                item_id=2,
                amount="25.00",
                kind="referral_reward_l1",
                balance_after="125.00",
            ),
            _item(
                item_id=1,
                amount="-0.07",
                kind="periodic_charge",
                balance_after="100.00",
            ),
        ),
        total=2,
        limit=8,
        offset=0,
        snapshot_id=42,
    )

    text = render_balance_history(page)

    assert "+25,00 ₽" in text
    assert "Реферальный бонус · 1 уровень" in text
    assert "−0,07 ₽" in text
    assert "Оплата VPN" in text
    assert "после операции 125,00 ₽" in text
    assert "UTC" in text
    assert "must-not-be-rendered" not in text


def test_balance_history_keyboard_bounds_pagination():
    first = balance_history_keyboard(offset=0, limit=8, total=20, snapshot_id=42)
    middle = balance_history_keyboard(offset=8, limit=8, total=20, snapshot_id=42)
    last = balance_history_keyboard(offset=16, limit=8, total=20, snapshot_id=42)

    assert [button.callback_data for button in first.inline_keyboard[0]] == [
        "balance_history:42:8"
    ]
    assert [button.callback_data for button in middle.inline_keyboard[0]] == [
        "balance_history:42:0",
        "balance_history:42:16",
    ]
    assert [button.callback_data for button in last.inline_keyboard[0]] == [
        "balance_history:42:8"
    ]
