import asyncio
import logging
from decimal import Decimal
from decimal import InvalidOperation as DecimalInvalidOperation

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
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

from ..keyboards import main_menu_keyboard
from ..ui import format_money, safe_callback_answer
from .base import AVAILABLE_AMOUNTS, billing_service, get_or_create_user, router

logger = logging.getLogger(__name__)
payment_events_router = Router(name="telegram-payment-events")

__all__ = [
    "cmd_topup",
    "pay_crypto",
    "pay_telegram",
    "got_topup_amount",
    "process_pre_checkout_query",
    "successful_payment_handler",
    "payment_events_router",
]


def _top_up_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{amount} ₽", callback_data=f"topup:{amount}")]
            for amount in AVAILABLE_AMOUNTS
        ]
    )


async def cmd_topup(message: Message) -> None:
    await get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer(
        "💳 <b>Пополнение баланса</b>\n\n"
        "Выберите сумму. Telegram покажет защищённую форму оплаты перед "
        "подтверждением. Для электронного чека форма попросит email и "
        "передаст его платёжному провайдеру:",
        reply_markup=_top_up_keyboard(),
    )


@router.callback_query(lambda c: c.data == "pay:crypto")
async def pay_crypto(callback: CallbackQuery) -> None:
    await callback.message.answer(
        "Оплата криптовалютой пока недоступна. Выберите оплату через Telegram."
    )
    await safe_callback_answer(callback)


@router.callback_query(lambda c: c.data == "pay:telegram")
async def pay_telegram(callback: CallbackQuery) -> None:
    await callback.message.answer(
        "Выберите сумму пополнения:",
        reply_markup=_top_up_keyboard(),
    )
    await safe_callback_answer(callback)


@router.callback_query(F.data.startswith("topup:"))
async def got_topup_amount(callback: CallbackQuery, bot, event_update: Update) -> None:
    try:
        amount = Decimal(callback.data.split(":", 1)[1])
        allowed_amounts = {Decimal(str(value)) for value in AVAILABLE_AMOUNTS}
        if amount not in allowed_amounts:
            raise ValueError
    except (DecimalInvalidOperation, IndexError, ValueError):
        await safe_callback_answer(
            callback,
            "Некорректная сумма.",
            show_alert=True,
        )
        return

    user = await get_or_create_user(callback.from_user.id, callback.from_user.username)
    intent = await billing_service.create_payment_intent(
        user_id=user.id,
        amount=amount,
        provider="telegram",
        currency="RUB",
        idempotency_key=f"telegram:invoice:update:{event_update.update_id}",
    )
    claimed = await billing_service.claim_payment_invoice_delivery(
        user_id=user.id,
        intent_id=intent.intent_id,
        provider="telegram",
    )
    if not claimed:
        await safe_callback_answer(
            callback,
            "Счёт для этого запроса уже создан. Если он не появился, "
            "нажмите сумму ещё раз.",
            show_alert=True,
        )
        return

    service = TelegramPayService(bot, settings.telegram_pay_token)
    try:
        await service.send_invoice(
            callback.from_user.id,
            intent.amount,
            payload=intent.payload,
            currency=intent.currency,
        )
    except TelegramAPIError:
        logger.warning(
            "Telegram invoice delivery failed after its attempt was claimed",
            extra={"intent_id": intent.intent_id, "user_id": user.id},
            exc_info=True,
        )
        try:
            await safe_callback_answer(
                callback,
                "Не удалось подтвердить отправку счёта. Нажмите сумму ещё раз.",
                show_alert=True,
            )
        except TelegramAPIError:
            # The callback acknowledgement is best-effort after an ambiguous
            # provider error. The durable update must still be acknowledged so
            # it cannot send the same invoice a second time.
            pass
        return
    await safe_callback_answer(callback)


@payment_events_router.pre_checkout_query()
async def process_pre_checkout_query(pcq: PreCheckoutQuery, bot) -> None:
    try:
        user = await get_or_create_user(pcq.from_user.id, pcq.from_user.username)
        await billing_service.validate_payment_intent(
            user_id=user.id,
            claim_id=pcq.id,
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


@payment_events_router.message(F.successful_payment)
async def successful_payment_handler(message: Message, bot) -> None:
    payment = message.successful_payment
    qty = Decimal(payment.total_amount) / Decimal(100)
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
    try:
        await bot.send_message(
            chat_id=message.from_user.id,
            text=(
                "✅ Платёж успешно завершён! Баланс пополнен на "
                f"{format_money(qty)} ₽."
            ),
            reply_markup=main_menu_keyboard(),
        )
    except TelegramAPIError:
        # The balance is already committed. A blocked private chat must not
        # turn a captured payment into an endlessly retried update.
        logger.warning(
            "Captured payment was credited but confirmation could not be delivered",
            extra={
                "telegram_payment_charge_id": payment.telegram_payment_charge_id,
                "user_id": message.from_user.id,
            },
            exc_info=True,
        )
