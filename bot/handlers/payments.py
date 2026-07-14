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
from core.exceptions import InvalidOperationError, UserNotFoundError
from core.services import TelegramPayService
from core.services.telegram_user_actions import TelegramActionAuditContext

from ..keyboards import main_menu_keyboard
from ..ui import format_money, safe_callback_answer
from .base import AVAILABLE_AMOUNTS, billing_service, get_or_create_user, router

logger = logging.getLogger(__name__)
payment_events_router = Router(name="telegram-payment-events")

PAYMENTS_DISABLED_TEXT = (
    "💳 Пополнение баланса временно приостановлено. "
    "Уже оплаченные счета будут зачислены автоматически. Попробуйте позже."
)
PAYMENTS_DISABLED_PRECHECKOUT_TEXT = (
    "Приём новых платежей временно приостановлен. Попробуйте позже."
)

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


async def cmd_topup(
    message: Message,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    if not settings.payments_enabled:
        if telegram_action_audit is not None:
            telegram_action_audit.record(
                "finance.topup_open",
                result="unavailable",
                metadata={"reason_code": "payments_disabled"},
            )
        await message.answer(
            PAYMENTS_DISABLED_TEXT,
            reply_markup=main_menu_keyboard(),
        )
        return
    await get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer(
        "💳 <b>Пополнение баланса</b>\n\n"
        "Выберите сумму. Telegram покажет защищённую форму оплаты перед "
        "подтверждением. Для электронного чека форма попросит email и "
        "передаст его платёжному провайдеру:",
        reply_markup=_top_up_keyboard(),
    )
    if telegram_action_audit is not None:
        telegram_action_audit.record("finance.topup_open")


@router.callback_query(lambda c: c.data == "pay:crypto")
async def pay_crypto(
    callback: CallbackQuery,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    await callback.message.answer(
        "Оплата криптовалютой пока недоступна. Выберите оплату через Telegram."
    )
    await safe_callback_answer(callback)
    if telegram_action_audit is not None:
        telegram_action_audit.record(
            "finance.payment_provider_select",
            result="unavailable",
            metadata={
                "provider": "crypto",
                "reason_code": "provider_unavailable",
            },
        )


@router.callback_query(lambda c: c.data == "pay:telegram")
async def pay_telegram(
    callback: CallbackQuery,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    if not settings.payments_enabled:
        if telegram_action_audit is not None:
            telegram_action_audit.record(
                "finance.payment_provider_select",
                result="unavailable",
                metadata={
                    "provider": "telegram",
                    "reason_code": "payments_disabled",
                },
            )
        await safe_callback_answer(
            callback,
            PAYMENTS_DISABLED_TEXT,
            show_alert=True,
        )
        return
    await callback.message.answer(
        "Выберите сумму пополнения:",
        reply_markup=_top_up_keyboard(),
    )
    await safe_callback_answer(callback)
    if telegram_action_audit is not None:
        telegram_action_audit.record(
            "finance.payment_provider_select",
            metadata={"provider": "telegram"},
        )


@router.callback_query(F.data.startswith("topup:"))
async def got_topup_amount(
    callback: CallbackQuery,
    bot,
    event_update: Update,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    if not settings.payments_enabled:
        if telegram_action_audit is not None:
            telegram_action_audit.record(
                "finance.payment_amount_select",
                result="unavailable",
                metadata={"reason_code": "payments_disabled"},
            )
        await safe_callback_answer(
            callback,
            PAYMENTS_DISABLED_TEXT,
            show_alert=True,
        )
        return
    try:
        amount = Decimal(callback.data.split(":", 1)[1])
        allowed_amounts = {Decimal(str(value)) for value in AVAILABLE_AMOUNTS}
        if amount not in allowed_amounts:
            raise ValueError
    except (DecimalInvalidOperation, IndexError, ValueError):
        if telegram_action_audit is not None:
            telegram_action_audit.record(
                "finance.payment_amount_select",
                result="invalid",
                metadata={"reason_code": "invalid_amount"},
            )
        await safe_callback_answer(
            callback,
            "Некорректная сумма.",
            show_alert=True,
        )
        return

    amount_rub = int(amount)
    if telegram_action_audit is not None:
        telegram_action_audit.record(
            "finance.payment_amount_select",
            result="handled",
            metadata={"amount_rub": amount_rub},
        )

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
        if telegram_action_audit is not None:
            telegram_action_audit.record(
                "finance.payment_amount_select",
                metadata={
                    "amount_rub": amount_rub,
                    "reason_code": "invoice_already_claimed",
                },
            )
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
    except InvalidOperationError:
        if telegram_action_audit is not None:
            telegram_action_audit.record(
                "finance.payment_amount_select",
                result="unavailable",
                metadata={
                    "amount_rub": amount_rub,
                    "reason_code": "payments_disabled",
                },
            )
        await safe_callback_answer(
            callback,
            PAYMENTS_DISABLED_TEXT,
            show_alert=True,
        )
        return
    except TelegramAPIError:
        if telegram_action_audit is not None:
            telegram_action_audit.record(
                "finance.payment_amount_select",
                result="failed",
                metadata={
                    "amount_rub": amount_rub,
                    "reason_code": "invoice_delivery_ambiguous",
                },
            )
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
    if telegram_action_audit is not None:
        telegram_action_audit.record(
            "finance.payment_amount_select",
            metadata={"amount_rub": amount_rub},
        )


@payment_events_router.pre_checkout_query()
async def process_pre_checkout_query(
    pcq: PreCheckoutQuery,
    bot,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    if not settings.payments_enabled:
        if telegram_action_audit is not None:
            telegram_action_audit.record(
                "finance.payment_pre_checkout",
                result="unavailable",
                metadata={"reason_code": "payments_disabled"},
            )
        await bot.answer_pre_checkout_query(
            pcq.id,
            ok=False,
            error_message=PAYMENTS_DISABLED_PRECHECKOUT_TEXT,
        )
        return
    try:
        user = await get_or_create_user(pcq.from_user.id, pcq.from_user.username)
        if user is None:
            raise UserNotFoundError("Unknown Telegram payer")
        await billing_service.validate_payment_intent(
            user_id=user.id,
            claim_id=pcq.id,
            payload=pcq.invoice_payload,
            amount=Decimal(pcq.total_amount) / Decimal(100),
            currency=pcq.currency,
            provider="telegram",
        )
    except Exception:
        if telegram_action_audit is not None:
            telegram_action_audit.record(
                "finance.payment_pre_checkout",
                result="rejected",
                metadata={"reason_code": "payment_validation_failed"},
            )
        await bot.answer_pre_checkout_query(
            pcq.id,
            ok=False,
            error_message="Не удалось проверить платёж. Создайте новый счёт.",
        )
        return
    await bot.answer_pre_checkout_query(pcq.id, ok=True)
    if telegram_action_audit is not None:
        telegram_action_audit.record("finance.payment_pre_checkout")


@payment_events_router.message(F.successful_payment)
async def successful_payment_handler(
    message: Message,
    bot,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    payment = message.successful_payment
    qty = Decimal(payment.total_amount) / Decimal(100)
    intent_id = (
        payment.invoice_payload.removeprefix("topup:")
        if payment.invoice_payload.startswith("topup:")
        else None
    )
    receipt = None
    for attempt in range(1, 6):
        try:
            user = await get_or_create_user(
                message.from_user.id,
                message.from_user.username,
            )
            if user is None:
                raise UserNotFoundError(
                    "Captured Telegram payment belongs to an unknown account"
                )
            receipt = await billing_service.record_telegram_payment(
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
    if telegram_action_audit is not None:
        telegram_action_audit.record(
            "finance.payment_successful",
            metadata={
                "reason_code": (
                    "payment_credited"
                    if getattr(receipt, "credited", True)
                    else "payment_replayed"
                )
            },
        )
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
