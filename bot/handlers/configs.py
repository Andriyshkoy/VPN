import os
import tempfile
import uuid

from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from core.exceptions import InsufficientBalanceError, ServiceError

from ..states import CreateConfig, RenameConfig
from .base import (
    billing_service,
    config_service,
    get_or_create_user as _get_or_create_user,
    is_admin,
    require_user,
    router,
    server_service,
    user_service,
)
from .keyboards import config_actions_kb

__all__ = [
    "cmd_configs",
    "cmd_create_config",
    "list_configs_cb",
    "start_create_config_cb",
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

CONFIGS_PER_PAGE = 8

# Backward-compat for tests that monkeypatch this symbol.
get_or_create_user = _get_or_create_user


def _config_list_callback(owner_id: int | None, page: int) -> str:
    if owner_id is None:
        return f"cfg:list:{page}"
    return f"cfg:list:{owner_id}:{page}"


async def _send_configs_list(
    target: Message | CallbackQuery,
    *,
    owner_id: int,
    page: int,
    include_create: bool,
    include_admin_back: bool,
) -> None:
    configs = await config_service.list(owner_id=owner_id)
    configs = sorted(configs, key=lambda c: (c.suspended, c.id))

    total = len(configs)
    start = page * CONFIGS_PER_PAGE
    end = start + CONFIGS_PER_PAGE
    page_configs = configs[start:end]

    if not page_configs:
        text = "У пользователя нет конфигураций" if include_admin_back else "У вас нет конфигураций"
        send_method = target.answer if isinstance(target, Message) else target.message.answer
        await send_method(text)
        if isinstance(target, CallbackQuery):
            await target.answer()
        return

    buttons = []
    for cfg in page_configs:
        title = cfg.display_name
        if cfg.suspended:
            title += " (приостановлена)"
        buttons.append([InlineKeyboardButton(text=title, callback_data=f"cfg:{cfg.id}")])

    nav_buttons = []
    if start > 0:
        nav_buttons.append(
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=_config_list_callback(owner_id if include_admin_back else None, page - 1),
            )
        )
    if end < total:
        nav_buttons.append(
            InlineKeyboardButton(
                text="Вперёд ➡️",
                callback_data=_config_list_callback(owner_id if include_admin_back else None, page + 1),
            )
        )
    if nav_buttons:
        buttons.append(nav_buttons)
    if include_create:
        buttons.append([InlineKeyboardButton(text="➕ Новый конфиг", callback_data="cfg:create")])

    back_text = "🛠 Админка" if include_admin_back else "👤 Личный кабинет"
    back_cb = "admin:home" if include_admin_back else "cabinet:home"
    buttons.append([InlineKeyboardButton(text=back_text, callback_data=back_cb)])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    text = "Конфигурации:" if include_admin_back else "Ваши конфигурации:"
    send_method = target.answer if isinstance(target, Message) else target.message.edit_text
    await send_method(text, reply_markup=kb)
    if isinstance(target, CallbackQuery):
        await target.answer()


@router.message(Command("configs"))
async def cmd_configs(message: Message) -> None:
    user = await require_user(message)
    if not user:
        return
    await _send_configs_list(
        message,
        owner_id=user.id,
        page=0,
        include_create=True,
        include_admin_back=False,
    )


@router.callback_query(lambda c: c.data and c.data.startswith("cfg:list"))
async def list_configs_cb(callback: CallbackQuery) -> None:
    user = await require_user(callback)
    if not user:
        return

    parts = callback.data.split(":")
    owner_id = None
    page = 0
    try:
        if len(parts) == 3:
            page = int(parts[2])
        elif len(parts) == 4:
            owner_id = int(parts[2])
            page = int(parts[3])
        else:
            raise ValueError
    except (ValueError, TypeError):
        await callback.answer("Некорректные данные", show_alert=True)
        return

    if owner_id is None:
        owner_id = user.id
        include_admin_back = False
        include_create = True
    else:
        if not is_admin(callback.from_user.id):
            await callback.answer("Недостаточно прав", show_alert=True)
            return
        include_admin_back = True
        include_create = False

    await _send_configs_list(
        callback,
        owner_id=owner_id,
        page=page,
        include_create=include_create,
        include_admin_back=include_admin_back,
    )


@router.message(Command("create_config"))
async def cmd_create_config(message: Message, state: FSMContext) -> None:
    user = await require_user(message)
    if not user:
        return
    await _send_create_config(message, state)


@router.message(F.text == "➕ Новый конфиг")
async def start_create_config_message(message: Message, state: FSMContext) -> None:
    user = await require_user(message)
    if not user:
        return
    await _send_create_config(message, state)


@router.callback_query(F.data == "cfg:create")
async def start_create_config_cb(callback: CallbackQuery, state: FSMContext) -> None:
    user = await require_user(callback)
    if not user:
        return
    await _send_create_config(callback, state)
    await callback.answer()


async def _send_create_config(target: Message | CallbackQuery, state: FSMContext) -> None:
    servers = await server_service.list()
    if not servers:
        send_method = target.answer if isinstance(target, Message) else target.message.answer
        await send_method("Нет доступных серверов")
        return
    settings = await billing_service.get_settings()
    buttons = [
        [InlineKeyboardButton(text=" ".join([s.location, s.name]), callback_data=f"server:{s.id}")]
        for s in servers
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    send_method = target.answer if isinstance(target, Message) else target.message.answer
    await send_method(
        "Выберите сервер для новой конфигурации.\n\n"
        f"Стоимость создания конфигурации — {settings.config_creation_cost} рублей (списывается сразу). "
        f"Ежемесячная плата за использование составляет {settings.monthly_config_cost} рублей и списывается постепенно.",
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
    user = await require_user(message)
    if not user:
        await state.clear()
        return
    unique_name = uuid.uuid4().hex
    try:
        cfg = await billing_service.create_paid_config(
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
    await message.answer("Конфигурация создана")
    await message.answer(
        "Управляйте конфигурациями через личный кабинет и кнопки меню. "
        "Инструкцию по подключению можно открыть кнопкой «ℹ️ Инструкция»."
    )
    await state.clear()


@router.callback_query(
    lambda c: c.data
    and c.data.startswith("cfg:")
    and not c.data.startswith("cfg:list")
    and not c.data.startswith("cfg:create")
)
async def show_config(callback: CallbackQuery) -> None:
    config_id = int(callback.data.split(":", 1)[1])
    user = await require_user(callback)
    if not user:
        return
    cfg = await config_service.get(config_id)
    if not cfg:
        await callback.answer("Конфигурация не найдена", show_alert=True)
        return
    if cfg.owner_id != user.id and not is_admin(callback.from_user.id):
        await callback.answer("Конфигурация не найдена", show_alert=True)
        return
    server = await server_service.get(cfg.server_id)
    text = (
        f"<b>{cfg.display_name}</b>\n"
        f"Сервер: {server.name} ({server.location})\n"
        f"Статус: {'приостановлена' if cfg.suspended else 'активна'}"
    )
    kb = config_actions_kb(
        config_id=cfg.id,
        suspended=cfg.suspended,
        include_admin_back=is_admin(callback.from_user.id),
    )
    await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("sus:"))
async def suspend_config_cb(callback: CallbackQuery) -> None:
    config_id = int(callback.data.split(":", 1)[1])
    user = await require_user(callback)
    if not user:
        return
    cfg = await config_service.get(config_id)
    if not cfg:
        await callback.answer("Конфигурация не найдена", show_alert=True)
        return
    if cfg.owner_id != user.id and not is_admin(callback.from_user.id):
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
    user = await require_user(callback)
    if not user:
        return
    cfg = await config_service.get(config_id)
    if not cfg:
        await callback.answer("Конфигурация не найдена", show_alert=True)
        return
    if cfg.owner_id != user.id and not is_admin(callback.from_user.id):
        await callback.answer("Конфигурация не найдена", show_alert=True)
        return
    owner = await user_service.get(cfg.owner_id)
    if owner is None or owner.balance <= 0:
        await callback.message.answer("Недостаточно средств. Пополните баланс")
        await callback.answer()
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
    user = await require_user(callback)
    if not user:
        return
    cfg = await config_service.get(config_id)
    if not cfg:
        await callback.answer("Конфигурация не найдена", show_alert=True)
        return
    if cfg.owner_id != user.id and not is_admin(callback.from_user.id):
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
    user = await require_user(callback)
    if not user:
        return
    cfg = await config_service.get(config_id)
    if not cfg:
        await callback.answer("Конфигурация не найдена", show_alert=True)
        return
    if cfg.owner_id != user.id and not is_admin(callback.from_user.id):
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
    user = await require_user(message)
    if not user:
        await state.clear()
        return
    cfg = await config_service.get(config_id)
    if not cfg:
        await message.answer("Конфигурация не найдена")
        await state.clear()
        return
    if cfg.owner_id != user.id and not is_admin(message.from_user.id):
        await message.answer("Конфигурация не найдена")
        await state.clear()
        return
    try:
        await config_service.rename_config(config_id, message.text)
        await message.answer("Конфигурация переименована")
    except ServiceError:
        await message.answer("Произошла ошибка. Попробуйте позже")
    await state.clear()
