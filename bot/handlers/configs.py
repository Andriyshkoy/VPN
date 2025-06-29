import os
import tempfile
import uuid

from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from core.config import settings
from core.exceptions import InsufficientBalanceError, ServiceError

from .base import (
    router,
    billing_service,
    config_service,
    server_service,
    get_or_create_user,
)
from ..states import CreateConfig, RenameConfig

__all__ = [
    "cmd_configs",
    "cmd_create_config",
    "choose_server",
    "got_name",
    "show_config",
    "suspend_config_cb",
    "unsuspend_config_cb",
    "delete_config_cb",
    "download_config_cb",
    "rename_config_cb",
    "got_new_name",
]


@router.message(Command("configs"))
async def cmd_configs(message: Message) -> None:
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    active = await config_service.list_active(owner_id=user.id)
    suspended = await config_service.list_suspended(owner_id=user.id)
    configs = active + suspended
    if not configs:
        await message.answer("У вас нет конфигураций")
        return
    buttons = []
    for cfg in configs:
        title = cfg.display_name
        if cfg.suspended:
            title += " (приостановлена)"
        buttons.append([InlineKeyboardButton(text=title, callback_data=f"cfg:{cfg.id}")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("Ваши конфигурации:", reply_markup=kb)


@router.message(Command("create_config"))
async def cmd_create_config(message: Message, state: FSMContext) -> None:
    await get_or_create_user(message.from_user.id, message.from_user.username)
    servers = await server_service.list()
    if not servers:
        await message.answer("Нет доступных серверов")
        return
    buttons = [
        [InlineKeyboardButton(text=" ".join([s.location, s.name]), callback_data=f"server:{s.id}")]
        for s in servers
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(
        "Выберите сервер для новой конфигурации.\n\n"
        "Стоимость создания конфигурации — 10 рублей (списывается сразу). "
        "Ежемесячная плата за использование составляет 50 рублей и списывается постепенно.",
        reply_markup=kb,
    )
    await state.set_state(CreateConfig.choosing_server)


@router.callback_query(lambda c: c.data and c.data.startswith("server:"))
async def choose_server(callback: CallbackQuery, state: FSMContext) -> None:
    server_id = int(callback.data.split(":", 1)[1])
    await state.update_data(server_id=server_id)
    await callback.message.answer(
        "📝 *Введите название для конфигурации*\n\n"
        "Это имя будет использоваться для идентификации вашей конфигурации в VPN-клиенте, "
        "а также будет отображаться в списке ваших конфигураций.\n\n"
        "✏️ Вы всегда сможете изменить его позже."
    )
    await state.set_state(CreateConfig.entering_name)
    await callback.answer()


@router.message(CreateConfig.entering_name)
async def got_name(message: Message, state: FSMContext, bot) -> None:
    data = await state.get_data()
    server_id = data.get("server_id")
    if not server_id:
        await message.answer("Сервер не выбран")
        await state.clear()
        return
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    unique_name = uuid.uuid4().hex
    try:
        cfg = await billing_service.create_paid_config(
            server_id=server_id,
            owner_id=user.id,
            name=unique_name,
            display_name=message.text,
            creation_cost=settings.config_creation_cost,
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
    await message.answer("Конфигурация создана")
    await message.answer(
        "Вы можете управлять конфигурацией через команду /configs или "
        "Ознакомиться с инструкцией по подключению к VPN с помощью команды /how_to_use."
    )
    await state.clear()


@router.callback_query(lambda c: c.data and c.data.startswith("cfg:"))
async def show_config(callback: CallbackQuery) -> None:
    config_id = int(callback.data.split(":", 1)[1])
    user = await get_or_create_user(callback.from_user.id, callback.from_user.username)
    cfg = await config_service.get(config_id)
    if not cfg or cfg.owner_id != user.id:
        await callback.answer("Конфигурация не найдена", show_alert=True)
        return
    server = await server_service.get(cfg.server_id)
    text = (
        f"<b>{cfg.display_name}</b>\n"
        f"Сервер: {server.name} ({server.location})\n"
        f"Статус: {'приостановлена' if cfg.suspended else 'активна'}"
    )
    buttons = []
    buttons.append([InlineKeyboardButton(text="Удалить", callback_data=f"del:{cfg.id}")])
    buttons.append([InlineKeyboardButton(text="Скачать", callback_data=f"dl:{cfg.id}")])
    buttons.append([InlineKeyboardButton(text="Переименовать", callback_data=f"rn:{cfg.id}")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("sus:"))
async def suspend_config_cb(callback: CallbackQuery) -> None:
    config_id = int(callback.data.split(":", 1)[1])
    user = await get_or_create_user(callback.from_user.id, callback.from_user.username)
    cfg = await config_service.get(config_id)
    if not cfg or cfg.owner_id != user.id:
        await callback.answer("Конфигурация не найдена", show_alert=True)
        return
    try:
        await config_service.suspend_config(config_id)
        await callback.message.answer("Конфигурация приостановлена")
    except ServiceError:
        await callback.message.answer("Произошла ошибка. Попробуйте позже")
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("uns:"))
async def unsuspend_config_cb(callback: CallbackQuery) -> None:
    config_id = int(callback.data.split(":", 1)[1])
    user = await get_or_create_user(callback.from_user.id, callback.from_user.username)
    if user.balance <= 0:
        await callback.message.answer("Недостаточно средств. Пополните баланс")
        await callback.answer()
        return
    cfg = await config_service.get(config_id)
    if not cfg or cfg.owner_id != user.id:
        await callback.answer("Конфигурация не найдена", show_alert=True)
        return
    try:
        await config_service.unsuspend_config(config_id)
        await callback.message.answer("Конфигурация возобновлена")
    except ServiceError:
        await callback.message.answer("Произошла ошибка. Попробуйте позже")
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("del:"))
async def delete_config_cb(callback: CallbackQuery) -> None:
    config_id = int(callback.data.split(":", 1)[1])
    user = await get_or_create_user(callback.from_user.id, callback.from_user.username)
    cfg = await config_service.get(config_id)
    if not cfg or cfg.owner_id != user.id:
        await callback.answer("Конфигурация не найдена", show_alert=True)
        return
    try:
        await config_service.revoke_config(config_id)
        await callback.message.answer("Конфигурация удалена")
    except ServiceError:
        await callback.message.answer("Произошла ошибка. Попробуйте позже")
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("dl:"))
async def download_config_cb(callback: CallbackQuery, bot) -> None:
    config_id = int(callback.data.split(":", 1)[1])
    user = await get_or_create_user(callback.from_user.id, callback.from_user.username)
    cfg = await config_service.get(config_id)
    if not cfg or cfg.owner_id != user.id:
        await callback.answer("Конфигурация не найдена", show_alert=True)
        return
    try:
        content = await config_service.download_config(config_id)
    except ServiceError:
        await callback.message.answer("Произошла ошибка. Попробуйте позже")
        await callback.answer()
        return
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        await bot.send_document(
            callback.message.chat.id,
            FSInputFile(tmp_path, filename=f"{cfg.display_name}.ovpn"),
        )
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("rn:"))
async def rename_config_cb(callback: CallbackQuery, state: FSMContext) -> None:
    config_id = int(callback.data.split(":", 1)[1])
    await state.update_data(config_id=config_id)
    await callback.message.answer("Введите новое название")
    await state.set_state(RenameConfig.entering_name)
    await callback.answer()


@router.message(RenameConfig.entering_name)
async def got_new_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    config_id = data.get("config_id")
    if not config_id:
        await message.answer("Конфигурация не выбрана")
        await state.clear()
        return
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    cfg = await config_service.get(config_id)
    if not cfg or cfg.owner_id != user.id:
        await message.answer("Конфигурация не найдена")
        await state.clear()
        return
    try:
        await config_service.rename_config(config_id, message.text)
        await message.answer("Конфигурация переименована")
    except ServiceError:
        await message.answer("Произошла ошибка. Попробуйте позже")
    await state.clear()
