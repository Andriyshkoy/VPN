from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import Message

from ..keyboards import main_menu_keyboard


async def unknown_text(message: Message) -> None:
    await message.answer(
        "Не понял сообщение. Выберите действие на клавиатуре 👇",
        reply_markup=main_menu_keyboard(),
    )


def register(router: Router) -> None:
    router.message(StateFilter(None), F.text)(unknown_text)
