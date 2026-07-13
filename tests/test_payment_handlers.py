from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramNetworkError

from bot.handlers import payments
from core.exceptions import InvalidOperationError
from core.services.billing import PaymentIntent
from core.services.payments import TelegramPayService


class DummyBot:
    def __init__(self):
        self.pre_checkout_answers = []
        self.messages = []

    async def answer_pre_checkout_query(self, query_id, **kwargs):
        self.pre_checkout_answers.append((query_id, kwargs))

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)


class DummyCallback:
    data = "topup:100"
    from_user = SimpleNamespace(id=123, username="alice")
    message = SimpleNamespace(chat=SimpleNamespace(id=456))

    async def answer(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_telegram_invoice_disables_payment_from_forwarded_copy():
    sent = {}

    class InvoiceBot:
        async def send_invoice(self, **kwargs):
            sent.update(kwargs)

    await TelegramPayService(InvoiceBot(), "provider-token").send_invoice(
        123,
        Decimal("100.00"),
        payload="topup:intent-id",
    )

    assert sent["start_parameter"] == "topup"


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

    async def claim_delivery(**kwargs):
        sent["claim_args"] = kwargs
        return True

    class PayService:
        def __init__(self, bot, token):
            sent["token"] = token

        async def send_invoice(self, chat_id, amount, **kwargs):
            sent["invoice"] = (chat_id, amount, kwargs)

    monkeypatch.setattr(payments, "get_or_create_user", get_user)
    monkeypatch.setattr(
        payments.billing_service, "create_payment_intent", create_intent
    )
    monkeypatch.setattr(
        payments.billing_service,
        "claim_payment_invoice_delivery",
        claim_delivery,
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
    assert sent["claim_args"] == {
        "user_id": user.id,
        "intent_id": intent.intent_id,
        "provider": "telegram",
    }
    assert sent["invoice"] == (
        123,
        Decimal("100.00"),
        {"payload": "topup:intent-id", "currency": "RUB"},
    )


@pytest.mark.asyncio
async def test_topup_replay_does_not_send_a_second_invoice(monkeypatch):
    user = SimpleNamespace(id=7)
    intent = PaymentIntent(
        intent_id="intent-id",
        payload="topup:intent-id",
        provider="telegram",
        amount=Decimal("100.00"),
        currency="RUB",
    )
    claim_results = iter((True, False))
    invoices = []

    async def get_user(*args, **kwargs):
        return user

    async def create_intent(**kwargs):
        return intent

    async def claim_delivery(**kwargs):
        return next(claim_results)

    class PayService:
        def __init__(self, bot, token):
            pass

        async def send_invoice(self, chat_id, amount, **kwargs):
            invoices.append((chat_id, amount, kwargs))

    monkeypatch.setattr(payments, "get_or_create_user", get_user)
    monkeypatch.setattr(
        payments.billing_service, "create_payment_intent", create_intent
    )
    monkeypatch.setattr(
        payments.billing_service,
        "claim_payment_invoice_delivery",
        claim_delivery,
    )
    monkeypatch.setattr(payments, "TelegramPayService", PayService)

    update = SimpleNamespace(update_id=7001)
    await payments.got_topup_amount(DummyCallback(), DummyBot(), update)
    await payments.got_topup_amount(DummyCallback(), DummyBot(), update)

    assert len(invoices) == 1


@pytest.mark.asyncio
async def test_ambiguous_invoice_error_is_not_automatically_retried(monkeypatch):
    class RecordingCallback(DummyCallback):
        def __init__(self):
            self.answers = []

        async def answer(self, *args, **kwargs):
            self.answers.append((args, kwargs))

    user = SimpleNamespace(id=7)
    intent = PaymentIntent(
        intent_id="intent-id",
        payload="topup:intent-id",
        provider="telegram",
        amount=Decimal("100.00"),
        currency="RUB",
    )
    claim_results = iter((True, False))
    send_attempts = 0

    async def get_user(*args, **kwargs):
        return user

    async def create_intent(**kwargs):
        return intent

    async def claim_delivery(**kwargs):
        return next(claim_results)

    class PayService:
        def __init__(self, bot, token):
            pass

        async def send_invoice(self, chat_id, amount, **kwargs):
            nonlocal send_attempts
            send_attempts += 1
            raise TelegramNetworkError(
                method=None,
                message="connection reset after send",
            )

    monkeypatch.setattr(payments, "get_or_create_user", get_user)
    monkeypatch.setattr(
        payments.billing_service, "create_payment_intent", create_intent
    )
    monkeypatch.setattr(
        payments.billing_service,
        "claim_payment_invoice_delivery",
        claim_delivery,
    )
    monkeypatch.setattr(payments, "TelegramPayService", PayService)

    first_callback = RecordingCallback()
    update = SimpleNamespace(update_id=7001)
    await payments.got_topup_amount(first_callback, DummyBot(), update)
    await payments.got_topup_amount(RecordingCallback(), DummyBot(), update)

    assert send_attempts == 1
    assert first_callback.answers == [
        (
            ("Не удалось подтвердить отправку счёта. Нажмите сумму ещё раз.",),
            {"show_alert": True},
        )
    ]


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
async def test_pre_checkout_only_approves_first_claim(monkeypatch):
    claimed_id = None

    async def get_user(*args, **kwargs):
        return SimpleNamespace(id=7)

    async def claim(**kwargs):
        nonlocal claimed_id
        if claimed_id is None:
            claimed_id = kwargs["claim_id"]
        elif kwargs["claim_id"] != claimed_id:
            raise InvalidOperationError("already claimed")
        return PaymentIntent(
            intent_id="intent-id",
            payload="topup:intent-id",
            provider="telegram",
            amount=Decimal("100.00"),
            currency="RUB",
        )

    monkeypatch.setattr(payments, "get_or_create_user", get_user)
    monkeypatch.setattr(payments.billing_service, "validate_payment_intent", claim)
    bot = DummyBot()
    query = SimpleNamespace(
        id="query-1",
        from_user=SimpleNamespace(id=123, username="alice"),
        invoice_payload="topup:intent-id",
        total_amount=10000,
        currency="RUB",
    )

    await payments.process_pre_checkout_query(query, bot)
    await payments.process_pre_checkout_query(query, bot)
    query.id = "query-2"
    await payments.process_pre_checkout_query(query, bot)

    assert bot.pre_checkout_answers == [
        ("query-1", {"ok": True}),
        ("query-1", {"ok": True}),
        (
            "query-2",
            {
                "ok": False,
                "error_message": ("Не удалось проверить платёж. Создайте новый счёт."),
            },
        ),
    ]


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

    message = SimpleNamespace(
        successful_payment=SuccessfulPayment(),
        from_user=SimpleNamespace(id=123, username="alice"),
    )
    bot = DummyBot()
    monkeypatch.setattr(payments, "get_or_create_user", get_user)
    monkeypatch.setattr(payments.billing_service, "record_telegram_payment", record)

    await payments.successful_payment_handler(message, bot)

    assert captured["user_id"] == 7
    assert captured["telegram_payment_charge_id"] == "telegram-charge"
    assert captured["provider_payment_charge_id"] == "provider-charge"
    assert captured["total_amount_minor"] == 1234
    assert captured["intent_id"] == "intent-id"
    assert bot.messages[0]["chat_id"] == message.from_user.id
    assert bot.messages[0]["text"] == (
        "✅ Платёж успешно завершён! Баланс пополнен на 12,34 ₽."
    )
