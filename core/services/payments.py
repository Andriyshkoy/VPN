from __future__ import annotations
import json
from typing import Optional

from aiogram import Bot
from aiogram.types import LabeledPrice


class TelegramPayService:
    """Service to send invoices via Telegram Payments with fiscalization"""

    def __init__(self, bot: Bot, provider_token: str) -> None:
        self._bot = bot
        self._token = provider_token

    async def send_invoice(
        self,
        chat_id: int,
        amount: float,
        *,
        title: str = "Пополнение баланса",
        description: str = "Оплата через Telegram Pay",
        payload: str = "topup",
        currency: str = "RUB",
    ) -> None:
        receipt_data = {
            "receipt": {
                "items": [
                    {
                        "description": title[:128],
                        "quantity": "1",
                        "amount": {
                            "value": f"{amount:.2f}",
                            "currency": currency
                        },
                        "vat_code": 1,
                    }
                ]
            }
        }

        provider_data = json.dumps(receipt_data, ensure_ascii=False)

        prices = [LabeledPrice(label=title, amount=int(amount * 100))]
        await self._bot.send_invoice(
            chat_id=chat_id,
            title=title,
            description=description,
            payload=payload,
            provider_token=self._token,
            currency=currency,
            prices=prices,
            provider_data=provider_data,
            send_email_to_provider=True,
            need_email=True
        )


class CryptoPaymentService:
    """Placeholder for future crypto payment integration."""

    async def create_payment(self, user_id: int, amount: float) -> None:
        # TODO: implement real crypto payment processing
        pass
