import asyncio
import logging
from decimal import Decimal
from decimal import InvalidOperation as DecimalInvalidOperation

from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PreCheckoutQuery,
    Update,
)
from sqlalchemy.exc import SQLAlchemyError

from core.config import settings
from core.services import TelegramPayService

from .base import AVAILABLE_AMOUNTS, billing_service, get_or_create_user, router

logger = logging.getLogger(__name__)

__all__ = [
    "cmd_topup",
    "pay_crypto",
    "pay_telegram",
    "got_topup_amount",
    "process_pre_checkout_query",
    "successful_payment_handler",
]


@router.message(Command("topup"))
async def cmd_topup(message: Message) -> None:
    await get_or_create_user(message.from_user.id, message.from_user.username)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🪙 Пополнить криптовалютой",
                    callback_data="pay:crypto",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💳 Оплатить картой (Telegram Pay)",
                    callback_data="pay:telegram",
                )
            ],
        ]
    )

    await message.answer(
        "💸 <b>Пополнение баланса</b>\n\n" "Выберите удобный способ оплаты:",
        reply_markup=kb,
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data == "pay:crypto")
async def pay_crypto(callback: CallbackQuery) -> None:
    await callback.message.answer("Оплата криптовалютой скоро появится!")
    await callback.answer()


@router.callback_query(lambda c: c.data == "pay:telegram")
async def pay_telegram(callback: CallbackQuery, state: FSMContext, bot) -> None:
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{amt} ₽", callback_data=f"topup:{amt}")]
            for amt in AVAILABLE_AMOUNTS
        ]
    )
    await callback.message.answer("Выберите сумму пополнения:", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("topup:"))
async def got_topup_amount(callback: CallbackQuery, bot, event_update: Update) -> None:
    try:
        amount = Decimal(callback.data.split(":", 1)[1])
        allowed_amounts = {Decimal(str(value)) for value in AVAILABLE_AMOUNTS}
        if amount not in allowed_amounts:
            raise ValueError
    except (DecimalInvalidOperation, IndexError, ValueError):
        await callback.answer("Некорректная сумма.", show_alert=True)
        return

    user = await get_or_create_user(callback.from_user.id, callback.from_user.username)
    intent = await billing_service.create_payment_intent(
        user_id=user.id,
        amount=amount,
        provider="telegram",
        currency="RUB",
        idempotency_key=f"telegram:invoice:update:{event_update.update_id}",
    )
    service = TelegramPayService(bot, settings.telegram_pay_token)
    await service.send_invoice(
        callback.message.chat.id,
        intent.amount,
        payload=intent.payload,
        currency=intent.currency,
    )


@router.pre_checkout_query()
async def process_pre_checkout_query(pcq: PreCheckoutQuery, bot) -> None:
    try:
        user = await get_or_create_user(pcq.from_user.id, pcq.from_user.username)
        await billing_service.validate_payment_intent(
            user_id=user.id,
            payload=pcq.invoice_payload,
            amount=Decimal(pcq.total_amount) / Decimal(100),
            currency=pcq.currency,
            provider="telegram",
        )
    except Exception:
        await bot.answer_pre_checkout_query(
            pcq.id,
            ok=False,
            error_message="Не удалось проверить платёж. Создайте новый счёт.",
        )
        return
    await bot.answer_pre_checkout_query(pcq.id, ok=True)


@router.message(F.successful_payment)
async def successful_payment_handler(message: Message) -> None:
    payment = message.successful_payment
    # Preserve the existing Telegram success-message formatting.
    qty = payment.total_amount / 100
    intent_id = (
        payment.invoice_payload.removeprefix("topup:")
        if payment.invoice_payload.startswith("topup:")
        else None
    )
    for attempt in range(1, 6):
        try:
            user = await get_or_create_user(
                message.from_user.id,
                message.from_user.username,
            )
            await billing_service.record_telegram_payment(
                user_id=user.id,
                telegram_payment_charge_id=payment.telegram_payment_charge_id,
                provider_payment_charge_id=payment.provider_payment_charge_id,
                total_amount_minor=payment.total_amount,
                currency=payment.currency,
                payload=payment.invoice_payload,
                intent_id=intent_id,
                # Do not persist Telegram order/e-mail PII in accounting.
                raw_data={
                    "provider_payment_charge_id": payment.provider_payment_charge_id
                },
            )
            break
        except (SQLAlchemyError, ConnectionError, TimeoutError, OSError):
            if attempt >= 5:
                logger.exception(
                    "Captured Telegram payment could not be persisted",
                    extra={
                        "telegram_payment_charge_id": (
                            payment.telegram_payment_charge_id
                        ),
                        "attempts": attempt,
                    },
                )
                raise
            await asyncio.sleep(2 ** (attempt - 1))
    await message.answer(
        f"✅ Платёж успешно завершён! Баланс пополнен на {qty} рублей."
    )
