from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from bot.handlers import balance_history as balance_history_handler
from bot.handlers.balance_history import render_balance_history
from bot.keyboards import balance_actions_keyboard, balance_history_keyboard
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
    first = balance_history_keyboard(
        offset=0,
        limit=8,
        total=20,
        snapshot_id=42,
        direction="credit",
    )
    middle = balance_history_keyboard(
        offset=8,
        limit=8,
        total=20,
        snapshot_id=42,
        direction="credit",
    )
    last = balance_history_keyboard(
        offset=16,
        limit=8,
        total=20,
        snapshot_id=42,
        direction="credit",
    )

    assert [button.callback_data for button in first.inline_keyboard[1]] == [
        "balance_history:credit:42:8"
    ]
    assert [button.callback_data for button in middle.inline_keyboard[1]] == [
        "balance_history:credit:42:0",
        "balance_history:credit:42:16",
    ]
    assert [button.callback_data for button in last.inline_keyboard[1]] == [
        "balance_history:credit:42:8"
    ]


def test_balance_history_is_split_into_credit_and_debit_views():
    actions = balance_actions_keyboard()
    callbacks = [button.callback_data for button in actions.inline_keyboard[0]]
    assert callbacks == [
        "balance_history:credit:0",
        "balance_history:debit:0",
    ]

    credit_page = BalanceHistoryPage(
        items=(
            _item(
                item_id=1,
                amount="25.00",
                kind="provider_payment",
                balance_after="25.00",
            ),
        ),
        total=1,
        limit=8,
        offset=0,
        snapshot_id=42,
    )
    debit_page = BalanceHistoryPage(
        items=(),
        total=0,
        limit=8,
        offset=0,
        snapshot_id=42,
    )

    credit_text = render_balance_history(
        credit_page,
        direction="credit",
    )
    assert "<b>Пополнения и начисления</b>" in credit_text
    assert "Пополнения и начисления 1–1 из 1" in credit_text
    assert "Списаний пока нет" in render_balance_history(
        debit_page,
        direction="debit",
    )


class _CallbackMessage:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class _Callback:
    def __init__(self, data: str):
        self.data = data
        self.from_user = SimpleNamespace(id=101, username="alice")
        self.message = _CallbackMessage()
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        "balance_history:refund:0",
        "balance_history:credit:not-a-snapshot:0",
        "balance_history:debit:-1",
        "balance_history:credit:1:2:3",
    ],
)
async def test_balance_history_callback_rejects_unknown_direction_and_bad_cursor(data):
    callback = _Callback(data)

    await balance_history_handler.balance_history_callback(callback)

    assert callback.answers == [(("Не удалось открыть эту страницу.",), {})]
    assert callback.message.edits == []


@pytest.mark.asyncio
async def test_balance_history_callback_passes_direction_and_snapshot(monkeypatch):
    observed = {}
    page = BalanceHistoryPage(
        items=(),
        total=0,
        limit=8,
        offset=8,
        snapshot_id=42,
    )

    async def history(tg_id, username, **kwargs):
        observed.update(tg_id=tg_id, username=username, **kwargs)
        return page

    monkeypatch.setattr(
        balance_history_handler,
        "_history_for_telegram_user",
        history,
    )
    callback = _Callback("balance_history:debit:42:8")

    await balance_history_handler.balance_history_callback(callback)

    assert observed == {
        "tg_id": 101,
        "username": "alice",
        "offset": 8,
        "snapshot_id": 42,
        "direction": "debit",
    }
    text, kwargs = callback.message.edits[-1]
    assert "<b>Списания</b>" in text
    assert kwargs["reply_markup"].inline_keyboard[0][1].text.startswith("✅")
    assert callback.answers == [((), {})]


@pytest.mark.asyncio
async def test_balance_history_callback_keeps_legacy_cursor_compatible(monkeypatch):
    observed = {}
    page = BalanceHistoryPage(
        items=(),
        total=0,
        limit=8,
        offset=8,
        snapshot_id=42,
    )

    async def history(tg_id, username, **kwargs):
        observed.update(tg_id=tg_id, username=username, **kwargs)
        return page

    monkeypatch.setattr(
        balance_history_handler,
        "_history_for_telegram_user",
        history,
    )
    callback = _Callback("balance_history:42:8")

    await balance_history_handler.balance_history_callback(callback)

    assert observed == {
        "tg_id": 101,
        "username": "alice",
        "offset": 8,
        "snapshot_id": 42,
        "direction": None,
    }
    assert "<b>История баланса</b>" in callback.message.edits[-1][0]
    assert callback.answers == [((), {})]
