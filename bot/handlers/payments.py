from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PreCheckoutQuery,
)

from core.config import settings
from core.services import TelegramPayService

from .base import AVAILABLE_AMOUNTS, billing_service, get_or_create_user, router

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
        "💸 <b>Пополнение баланса</b>\n\n"
        "Выберите удобный способ оплаты:",
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
async def got_topup_amount(callback: CallbackQuery, bot) -> None:
    try:
        amount = float(callback.data.split(":")[1])
        assert amount in AVAILABLE_AMOUNTS
    except (ValueError, AssertionError):
        await callback.answer("Некорректная сумма.", show_alert=True)
        return

    service = TelegramPayService(bot, settings.telegram_pay_token)
    await service.send_invoice(callback.message.chat.id, amount)


@router.pre_checkout_query()
async def process_pre_checkout_query(pcq: PreCheckoutQuery, bot) -> None:
    await bot.answer_pre_checkout_query(pcq.id, ok=True)


@router.message(F.successful_payment)
async def successful_payment_handler(message: Message) -> None:
    qty = message.successful_payment.total_amount / 100
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    await billing_service.top_up(user.id, qty)
    await message.answer(
        f"✅ Платёж успешно завершён! Баланс пополнен на {qty} рублей."
    )
