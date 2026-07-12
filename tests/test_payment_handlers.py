from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from bot.handlers import payments
from core.exceptions import InvalidOperationError
from core.services.billing import PaymentIntent


class DummyBot:
    def __init__(self):
        self.pre_checkout_answers = []

    async def answer_pre_checkout_query(self, query_id, **kwargs):
        self.pre_checkout_answers.append((query_id, kwargs))


class DummyCallback:
    data = "topup:100"
    from_user = SimpleNamespace(id=123, username="alice")
    message = SimpleNamespace(chat=SimpleNamespace(id=456))

    async def answer(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_topup_invoice_uses_persisted_intent_without_ui_change(monkeypatch):
    user = SimpleNamespace(id=7)
    intent = PaymentIntent(
        intent_id="intent-id",
        payload="topup:intent-id",
        provider="telegram",
        amount=Decimal("100.00"),
        currency="RUB",
    )
    sent = {}

    async def get_user(*args, **kwargs):
        return user

    async def create_intent(**kwargs):
        sent["intent_args"] = kwargs
        return intent

    class PayService:
        def __init__(self, bot, token):
            sent["token"] = token

        async def send_invoice(self, chat_id, amount, **kwargs):
            sent["invoice"] = (chat_id, amount, kwargs)

    monkeypatch.setattr(payments, "get_or_create_user", get_user)
    monkeypatch.setattr(
        payments.billing_service, "create_payment_intent", create_intent
    )
    monkeypatch.setattr(payments, "TelegramPayService", PayService)

    await payments.got_topup_amount(
        DummyCallback(),
        DummyBot(),
        SimpleNamespace(update_id=7001),
    )

    assert sent["intent_args"] == {
        "user_id": user.id,
        "amount": Decimal("100"),
        "provider": "telegram",
        "currency": "RUB",
        "idempotency_key": "telegram:invoice:update:7001",
    }
    assert sent["invoice"] == (
        456,
        Decimal("100.00"),
        {"payload": "topup:intent-id", "currency": "RUB"},
    )


@pytest.mark.asyncio
async def test_pre_checkout_rejects_invoice_mismatch(monkeypatch):
    async def get_user(*args, **kwargs):
        return SimpleNamespace(id=7)

    async def reject(**kwargs):
        raise InvalidOperationError("mismatch")

    monkeypatch.setattr(payments, "get_or_create_user", get_user)
    monkeypatch.setattr(payments.billing_service, "validate_payment_intent", reject)
    bot = DummyBot()
    query = SimpleNamespace(
        id="query-1",
        from_user=SimpleNamespace(id=123, username="alice"),
        invoice_payload="topup:intent-id",
        total_amount=10000,
        currency="RUB",
    )

    await payments.process_pre_checkout_query(query, bot)

    assert bot.pre_checkout_answers[0][1]["ok"] is False


@pytest.mark.asyncio
async def test_successful_payment_passes_provider_ids_to_idempotent_core(monkeypatch):
    captured = {}

    async def get_user(*args, **kwargs):
        return SimpleNamespace(id=7)

    async def record(**kwargs):
        captured.update(kwargs)

    class SuccessfulPayment:
        total_amount = 1234
        currency = "RUB"
        invoice_payload = "topup:intent-id"
        telegram_payment_charge_id = "telegram-charge"
        provider_payment_charge_id = "provider-charge"

        def model_dump(self, **kwargs):
            return {"telegram_payment_charge_id": self.telegram_payment_charge_id}

    answers = []

    async def answer(text):
        answers.append(text)

    message = SimpleNamespace(
        successful_payment=SuccessfulPayment(),
        from_user=SimpleNamespace(id=123, username="alice"),
        answer=answer,
    )
    monkeypatch.setattr(payments, "get_or_create_user", get_user)
    monkeypatch.setattr(payments.billing_service, "record_telegram_payment", record)

    await payments.successful_payment_handler(message)

    assert captured["user_id"] == 7
    assert captured["telegram_payment_charge_id"] == "telegram-charge"
    assert captured["provider_payment_charge_id"] == "provider-charge"
    assert captured["total_amount_minor"] == 1234
    assert captured["intent_id"] == "intent-id"
    assert answers == ["✅ Платёж успешно завершён! Баланс пополнен на 12.34 рублей."]
