from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramNetworkError

from bot.handlers import payments
from core.exceptions import InvalidOperationError, UserNotFoundError
from core.services.billing import PaymentIntent
from core.services.payments import TelegramPayService
from core.services.telegram_user_actions import TelegramActionAuditContext


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
async def test_telegram_invoice_service_respects_payment_kill_switch(monkeypatch):
    sent = False

    class InvoiceBot:
        async def send_invoice(self, **kwargs):
            nonlocal sent
            sent = True

    monkeypatch.setattr(payments.settings, "payments_enabled", False)

    with pytest.raises(InvalidOperationError, match="temporarily disabled"):
        await TelegramPayService(InvoiceBot(), "provider-token").send_invoice(
            123,
            Decimal("100.00"),
            payload="topup:intent-id",
        )

    assert sent is False


@pytest.mark.asyncio
async def test_payment_kill_switch_blocks_topup_ui_and_stale_callbacks(monkeypatch):
    class RecordingMessage:
        from_user = SimpleNamespace(id=123, username="alice")

        def __init__(self):
            self.answers = []

        async def answer(self, text, **kwargs):
            self.answers.append((text, kwargs))

    class RecordingCallback(DummyCallback):
        def __init__(self, data):
            self.data = data
            self.answers = []

        async def answer(self, *args, **kwargs):
            self.answers.append((args, kwargs))

    async def unexpected_user_lookup(*args, **kwargs):
        raise AssertionError("disabled payment flow must not access the account")

    monkeypatch.setattr(payments.settings, "payments_enabled", False)
    monkeypatch.setattr(payments, "get_or_create_user", unexpected_user_lookup)

    message = RecordingMessage()
    await payments.cmd_topup(message)
    telegram_callback = RecordingCallback("pay:telegram")
    await payments.pay_telegram(telegram_callback)
    amount_callback = RecordingCallback("topup:100")
    await payments.got_topup_amount(
        amount_callback,
        DummyBot(),
        SimpleNamespace(update_id=7001),
    )

    assert message.answers[0][0] == payments.PAYMENTS_DISABLED_TEXT
    assert message.answers[0][1]["reply_markup"] is not None
    assert telegram_callback.answers == [
        ((payments.PAYMENTS_DISABLED_TEXT,), {"show_alert": True})
    ]
    assert amount_callback.answers == [
        ((payments.PAYMENTS_DISABLED_TEXT,), {"show_alert": True})
    ]


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

    audit = TelegramActionAuditContext(
        "finance.payment_amount_select",
        "handled",
        {},
    )
    await payments.got_topup_amount(
        DummyCallback(),
        DummyBot(),
        SimpleNamespace(update_id=7001),
        telegram_action_audit=audit,
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
    assert audit.action == "finance.payment_amount_select"
    assert audit.result == "completed"
    assert audit.metadata == {"amount_rub": 100}
    assert "intent-id" not in repr(audit.metadata)


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

    audit = TelegramActionAuditContext(
        "finance.payment_pre_checkout",
        "handled",
        {},
    )
    await payments.process_pre_checkout_query(
        query,
        bot,
        telegram_action_audit=audit,
    )

    assert bot.pre_checkout_answers[0][1]["ok"] is False
    assert audit.action == "finance.payment_pre_checkout"
    assert audit.result == "rejected"
    assert audit.metadata == {"reason_code": "payment_validation_failed"}
    assert "topup:intent-id" not in repr(audit.metadata)


@pytest.mark.asyncio
async def test_pre_checkout_rejects_when_payments_are_disabled(monkeypatch):
    async def unexpected_user_lookup(*args, **kwargs):
        raise AssertionError("disabled pre-checkout must not access the account")

    monkeypatch.setattr(payments.settings, "payments_enabled", False)
    monkeypatch.setattr(payments, "get_or_create_user", unexpected_user_lookup)
    bot = DummyBot()
    query = SimpleNamespace(
        id="query-disabled",
        from_user=SimpleNamespace(id=123, username="alice"),
        invoice_payload="topup:intent-id",
        total_amount=10000,
        currency="RUB",
    )

    await payments.process_pre_checkout_query(query, bot)

    assert bot.pre_checkout_answers == [
        (
            "query-disabled",
            {
                "ok": False,
                "error_message": payments.PAYMENTS_DISABLED_PRECHECKOUT_TEXT,
            },
        )
    ]


@pytest.mark.asyncio
async def test_pre_checkout_rejects_unknown_user_without_creating_payment(monkeypatch):
    async def get_user(*args, **kwargs):
        return None

    validated = False

    async def validate(**kwargs):
        nonlocal validated
        validated = True

    monkeypatch.setattr(payments, "get_or_create_user", get_user)
    monkeypatch.setattr(payments.billing_service, "validate_payment_intent", validate)
    bot = DummyBot()
    query = SimpleNamespace(
        id="query-unknown",
        from_user=SimpleNamespace(id=999, username="unknown"),
        invoice_payload="topup:intent-id",
        total_amount=10000,
        currency="RUB",
    )

    await payments.process_pre_checkout_query(query, bot)

    assert validated is False
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
    monkeypatch.setattr(payments.settings, "payments_enabled", False)
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


@pytest.mark.asyncio
async def test_captured_payment_for_unknown_user_fails_without_fabricating_account(
    monkeypatch,
):
    async def get_user(*args, **kwargs):
        return None

    recorded = False

    async def record(**kwargs):
        nonlocal recorded
        recorded = True

    class SuccessfulPayment:
        total_amount = 1234
        currency = "RUB"
        invoice_payload = "topup:intent-id"
        telegram_payment_charge_id = "telegram-charge"
        provider_payment_charge_id = "provider-charge"

    message = SimpleNamespace(
        successful_payment=SuccessfulPayment(),
        from_user=SimpleNamespace(id=999, username="unknown"),
    )
    bot = DummyBot()
    monkeypatch.setattr(payments, "get_or_create_user", get_user)
    monkeypatch.setattr(payments.billing_service, "record_telegram_payment", record)

    with pytest.raises(UserNotFoundError, match="unknown account"):
        await payments.successful_payment_handler(message, bot)

    assert recorded is False
    assert bot.messages == []
