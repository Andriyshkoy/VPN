from __future__ import annotations

import uuid
import os
import tempfile

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from core.db.unit_of_work import uow
from core.services import ConfigService, ServerService, UserService
from core.exceptions import InsufficientBalanceError, ServiceError

from .states import CreateConfig

router = Router()

user_service = UserService(uow)
server_service = ServerService(uow)
config_service = ConfigService(uow)


async def get_or_create_user(tg_id: int, username: str | None):
    return await user_service.register(tg_id, username=username)


@router.message(Command("start"))
async def cmd_start(message: Message):
    await get_or_create_user(message.from_user.id, message.from_user.username)
    welcome_text = (
        "👋 Добро пожаловать в VPN бот!\n\n"
        "Этот бот поможет вам создать и управлять вашими VPN конфигурациями.\n\n"
        "Основные команды:\n"
        "• /create_config - создать новую VPN конфигурацию\n"
        "• /configs - просмотр ваших активных конфигураций\n"
        "• /balance - проверить ваш баланс\n\n"
        "Для получения полной информации используйте /help"
    )
    await message.answer(welcome_text)


@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "📌 <b>Список доступных команд:</b>\n\n"
        "• /start - начало работы с ботом\n"
        "• /help - показать эту справку\n"
        "• /balance - проверить ваш текущий баланс\n"
        "• /topup - информация о пополнении баланса\n"
        "• /configs - список ваших активных VPN конфигураций\n"
        "• /create_config - создать новую VPN конфигурацию\n\n"
        "<b>Как пользоваться ботом:</b>\n"
        "1. Проверьте баланс с помощью /balance\n"
        "2. При необходимости пополните баланс через /topup\n"
        "3. Создайте конфигурацию используя /create_config\n"
        "4. Скачайте .ovpn файл и импортируйте его в ваш VPN клиент\n"
        "5. Просматривайте свои конфигурации через /configs"
    )
    await message.answer(help_text, parse_mode="HTML")


@router.message(Command("balance"))
async def cmd_balance(message: Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer(f"Your balance: {user.balance}")


@router.message(Command("topup"))
async def cmd_topup(message: Message):
    await get_or_create_user(message.from_user.id, message.from_user.username)
    await message.answer("Для пополнения баланса свяжитесь с администратором сервиса")


@router.message(Command("configs"))
async def cmd_configs(message: Message):
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    configs = await config_service.list_active(owner_id=user.id)
    if not configs:
        await message.answer("You have no active configs")
        return
    text = "Your configs:\n" + "\n".join(f"- {c.display_name}" for c in configs)
    await message.answer(text)


@router.message(Command("create_config"))
async def cmd_create_config(message: Message, state: FSMContext):
    await get_or_create_user(message.from_user.id, message.from_user.username)
    servers = await server_service.list()
    if not servers:
        await message.answer("No servers available")
        return
    buttons = [
        [InlineKeyboardButton(text=s.name, callback_data=f"server:{s.id}")] for s in servers
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("Choose server", reply_markup=kb)
    await state.set_state(CreateConfig.choosing_server)


@router.callback_query(lambda c: c.data and c.data.startswith("server:"))
async def choose_server(callback: CallbackQuery, state: FSMContext):
    server_id = int(callback.data.split(":", 1)[1])
    await state.update_data(server_id=server_id)
    await callback.message.answer("Send display name for config")
    await state.set_state(CreateConfig.entering_name)
    await callback.answer()


@router.message(CreateConfig.entering_name)
async def got_name(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    server_id = data.get("server_id")
    if not server_id:
        await message.answer("Server not chosen")
        await state.clear()
        return
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    unique_name = uuid.uuid4().hex
    try:
        cfg = await config_service.create_config(
            server_id=server_id,
            owner_id=user.id,
            name=unique_name,
            display_name=message.text,
        )
    except InsufficientBalanceError:
        await message.answer("Недостаточно средств. Пополните баланс")
        await state.clear()
        return
    except ServiceError:
        await message.answer("Произошла ошибка. Попробуйте позже")
        await state.clear()
        return
    try:
        content = await config_service.download_config(cfg.id)
    except ServiceError:
        await message.answer("Произошла ошибка. Попробуйте позже")
        await state.clear()
        return
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        await bot.send_document(
            message.chat.id,
            FSInputFile(tmp_path, filename=f"{message.text}.ovpn"),
        )
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    await message.answer("Config created")
    await state.clear()
