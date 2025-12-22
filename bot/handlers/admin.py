from __future__ import annotations

from decimal import Decimal

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from core.exceptions import InsufficientBalanceError, UserNotFoundError

from ..states import (
    AdminBalanceChange,
    AdminBillingSettings,
    AdminServerCreate,
    AdminUserSearch,
)
from .base import (
    billing_service,
    config_service,
    is_admin,
    require_user,
    router,
    server_service,
    user_service,
)
from .keyboards import admin_menu_kb, admin_settings_kb, admin_user_kb

__all__ = [
    "admin_menu_message",
    "admin_menu_callback",
]


async def _require_admin(target: Message | CallbackQuery):
    user = await require_user(target)
    if not user:
        return None
    if not is_admin(target.from_user.id):
        if isinstance(target, CallbackQuery):
            await target.answer("Недостаточно прав", show_alert=True)
        else:
            await target.answer("Недостаточно прав")
        return None
    return user


async def _send_admin_menu(target: Message | CallbackQuery) -> None:
    text = "🛠 <b>Админка</b>\nВыберите раздел:"
    send_method = target.answer if isinstance(target, Message) else target.message.edit_text
    await send_method(text, reply_markup=admin_menu_kb(), parse_mode="HTML")
    if isinstance(target, CallbackQuery):
        await target.answer()


@router.message(F.text == "🛠 Админка")
async def admin_menu_message(message: Message) -> None:
    if not await _require_admin(message):
        return
    await _send_admin_menu(message)


@router.callback_query(F.data == "admin:home")
async def admin_menu_callback(callback: CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    await _send_admin_menu(callback)


@router.callback_query(F.data == "admin:users")
async def admin_users_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    await state.set_state(AdminUserSearch.waiting_query)
    await callback.message.answer(
        "Введите TG ID или username пользователя (можно с @):"
    )
    await callback.answer()


async def _send_user_detail(target: Message | CallbackQuery, user) -> None:
    active_count = await config_service.count_active(user.id)
    suspended = await config_service.list_suspended(owner_id=user.id)
    username = f"@{user.username}" if user.username else "—"
    text = (
        "👤 <b>Пользователь</b>\n\n"
        f"ID: <code>{user.id}</code>\n"
        f"TG ID: <code>{user.tg_id}</code>\n"
        f"Username: {username}\n"
        f"Баланс: <b>{user.balance:.2f} ₽</b>\n"
        f"Конфиги: ✅ {active_count} | ⏸ {len(suspended)}\n"
    )
    send_method = target.answer if isinstance(target, Message) else target.message.edit_text
    await send_method(text, reply_markup=admin_user_kb(user.id), parse_mode="HTML")
    if isinstance(target, CallbackQuery):
        await target.answer()


@router.message(AdminUserSearch.waiting_query)
async def admin_user_search(message: Message, state: FSMContext) -> None:
    if not await _require_admin(message):
        await state.clear()
        return
    query = message.text.strip()
    if not query:
        await message.answer("Введите TG ID или username.")
        return
    query = query.lstrip("@")

    await state.clear()
    if query.isdigit():
        user = await user_service.get_by_tg_id(int(query))
        if not user:
            await message.answer("Пользователь не найден.")
            return
        await _send_user_detail(message, user)
        return

    users = await user_service.search_by_username(query, limit=10)
    if not users:
        await message.answer("Пользователь не найден.")
        return
    if len(users) == 1:
        await _send_user_detail(message, users[0])
        return

    buttons = [
        [
            InlineKeyboardButton(
                text=f"@{u.username}" if u.username else f"ID {u.tg_id}",
                callback_data=f"admin:user:{u.id}",
            )
        ]
        for u in users
    ]
    buttons.append([InlineKeyboardButton(text="⬅️ Админка", callback_data="admin:home")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("Найдены пользователи:", reply_markup=kb)


@router.callback_query(lambda c: c.data and c.data.startswith("admin:user:"))
async def admin_user_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    parts = callback.data.split(":")
    if len(parts) not in (3, 4):
        await callback.answer("Некорректные данные", show_alert=True)
        return
    try:
        user_id = int(parts[2])
    except ValueError:
        await callback.answer("Некорректный пользователь", show_alert=True)
        return

    if len(parts) == 4:
        action = parts[3]
        if action in ("topup", "withdraw"):
            await state.update_data(user_id=user_id, action=action)
            await state.set_state(AdminBalanceChange.waiting_amount)
            await callback.message.answer(
                "Введите сумму и источник через пробел (например: 100 telegram_pay).\n"
                "Если источник не указан, будет использован admin."
            )
            await callback.answer()
            return
        await callback.answer("Некорректное действие", show_alert=True)
        return

    user = await user_service.get(user_id)
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    await _send_user_detail(callback, user)


@router.message(AdminBalanceChange.waiting_amount)
async def admin_balance_change(message: Message, state: FSMContext) -> None:
    if not await _require_admin(message):
        await state.clear()
        return
    data = await state.get_data()
    user_id = data.get("user_id")
    action = data.get("action")
    if not user_id or action not in ("topup", "withdraw"):
        await message.answer("Некорректные данные.")
        await state.clear()
        return

    parts = message.text.replace(",", ".").split(maxsplit=1)
    try:
        amount = Decimal(parts[0])
    except (ArithmeticError, ValueError):
        await message.answer("Некорректная сумма.")
        return
    if amount <= 0:
        await message.answer("Сумма должна быть больше нуля.")
        return
    amount = amount.quantize(Decimal("0.01"))
    source = parts[1].strip() if len(parts) > 1 else "admin"

    try:
        if action == "topup":
            user = await billing_service.top_up(
                user_id, float(amount), source=source
            )
            await message.answer("Баланс пополнен.")
        else:
            user = await billing_service.withdraw(
                user_id, float(amount), source=source
            )
            await message.answer("Средства списаны.")
    except InsufficientBalanceError:
        await message.answer("Недостаточно средств у пользователя.")
        return
    except UserNotFoundError:
        await message.answer("Пользователь не найден.")
        await state.clear()
        return

    await state.clear()
    await _send_user_detail(message, user)


@router.callback_query(F.data == "admin:settings")
async def admin_settings(callback: CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    settings = await billing_service.get_settings()
    text = (
        "⚙️ <b>Тарифы и бонусы</b>\n\n"
        f"• Создание конфига: {settings.config_creation_cost} ₽\n"
        f"• Месячный тариф: {settings.monthly_config_cost} ₽\n"
        f"• Бонус 1-го депозита: {settings.referral_first_deposit_bonus_pct}%\n"
        f"• Бонус повторных депозитов: {settings.referral_recurring_bonus_pct}%\n"
    )
    await callback.message.edit_text(text, reply_markup=admin_settings_kb(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("admin:settings:"))
async def admin_settings_edit(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    field = callback.data.split(":", 2)[2]
    if field not in {
        "config_creation_cost",
        "monthly_config_cost",
        "referral_first_deposit_bonus_pct",
        "referral_recurring_bonus_pct",
    }:
        await callback.answer("Некорректный параметр", show_alert=True)
        return
    await state.update_data(field=field)
    await state.set_state(AdminBillingSettings.waiting_value)
    await callback.message.answer("Введите новое значение:")
    await callback.answer()


@router.message(AdminBillingSettings.waiting_value)
async def admin_settings_value(message: Message, state: FSMContext) -> None:
    if not await _require_admin(message):
        await state.clear()
        return
    data = await state.get_data()
    field = data.get("field")
    if not field:
        await message.answer("Некорректный параметр.")
        await state.clear()
        return
    try:
        value = Decimal(message.text.replace(",", "."))
    except (ArithmeticError, ValueError):
        await message.answer("Некорректное значение.")
        return
    if value < 0:
        await message.answer("Значение должно быть неотрицательным.")
        return
    if field.endswith("_pct") and value > 100:
        await message.answer("Процент должен быть не больше 100.")
        return

    kwargs = {field: float(value)}
    await billing_service.update_settings(**kwargs)
    await state.clear()
    await message.answer("Настройка обновлена.")


@router.callback_query(F.data == "admin:servers")
async def admin_servers(callback: CallbackQuery) -> None:
    if not await _require_admin(callback):
        return
    await _send_server_list(callback)


async def _send_server_list(target: Message | CallbackQuery) -> None:
    servers = await server_service.list()
    text = "🖥 <b>Серверы</b>\n\n"
    buttons = []
    if not servers:
        text += "Нет добавленных серверов."
    else:
        for srv in servers:
            title = f"{srv.location} · {srv.name}"
            buttons.append(
                [InlineKeyboardButton(text=title, callback_data=f"admin:server:{srv.id}")]
            )
    buttons.append([InlineKeyboardButton(text="➕ Добавить", callback_data="admin:server:add")])
    buttons.append([InlineKeyboardButton(text="⬅️ Админка", callback_data="admin:home")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    send_method = target.answer if isinstance(target, Message) else target.message.edit_text
    await send_method(text, reply_markup=kb, parse_mode="HTML")
    if isinstance(target, CallbackQuery):
        await target.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("admin:server:"))
async def admin_server_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _require_admin(callback):
        return
    parts = callback.data.split(":")
    if len(parts) == 3 and parts[2] == "add":
        await state.set_state(AdminServerCreate.name)
        await callback.message.answer("Введите название сервера:")
        await callback.answer()
        return

    if len(parts) >= 4 and parts[2] == "delete":
        server_id = parts[3]
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Удалить",
                        callback_data=f"admin:server:confirm:{server_id}",
                    )
                ],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:servers")],
            ]
        )
        await callback.message.answer("Удалить сервер?", reply_markup=kb)
        await callback.answer()
        return

    if len(parts) >= 4 and parts[2] == "confirm":
        try:
            server_id = int(parts[3])
        except ValueError:
            await callback.answer("Некорректный сервер", show_alert=True)
            return
        deleted = await server_service.delete(server_id)
        if deleted:
            await callback.message.answer("Сервер удалён.")
        else:
            await callback.message.answer("Сервер не найден.")
        await _send_server_list(callback)
        return

    if len(parts) == 3:
        try:
            server_id = int(parts[2])
        except ValueError:
            await callback.answer("Некорректный сервер", show_alert=True)
            return
        srv = await server_service.get(server_id)
        if not srv:
            await callback.answer("Сервер не найден", show_alert=True)
            return
        text = (
            "🖥 <b>Сервер</b>\n\n"
            f"ID: <code>{srv.id}</code>\n"
            f"Название: {srv.name}\n"
            f"Локация: {srv.location}\n"
            f"IP: {srv.ip}:{srv.port}\n"
            f"Host: {srv.host}\n"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Удалить",
                        callback_data=f"admin:server:delete:{srv.id}",
                    )
                ],
                [InlineKeyboardButton(text="⬅️ Серверы", callback_data="admin:servers")],
            ]
        )
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await callback.answer()
        return

    await callback.answer("Некорректные данные", show_alert=True)


@router.message(AdminServerCreate.name)
async def admin_server_name(message: Message, state: FSMContext) -> None:
    if not await _require_admin(message):
        await state.clear()
        return
    await state.update_data(name=message.text.strip())
    await state.set_state(AdminServerCreate.ip)
    await message.answer("Введите IP адрес сервера:")


@router.message(AdminServerCreate.ip)
async def admin_server_ip(message: Message, state: FSMContext) -> None:
    if not await _require_admin(message):
        await state.clear()
        return
    await state.update_data(ip=message.text.strip())
    await state.set_state(AdminServerCreate.port)
    await message.answer("Введите порт (например 22):")


@router.message(AdminServerCreate.port)
async def admin_server_port(message: Message, state: FSMContext) -> None:
    if not await _require_admin(message):
        await state.clear()
        return
    try:
        port = int(message.text.strip())
    except ValueError:
        await message.answer("Некорректный порт.")
        return
    await state.update_data(port=port)
    await state.set_state(AdminServerCreate.host)
    await message.answer("Введите host (например vpn.example.com):")


@router.message(AdminServerCreate.host)
async def admin_server_host(message: Message, state: FSMContext) -> None:
    if not await _require_admin(message):
        await state.clear()
        return
    await state.update_data(host=message.text.strip())
    await state.set_state(AdminServerCreate.location)
    await message.answer("Введите локацию (например DE):")


@router.message(AdminServerCreate.location)
async def admin_server_location(message: Message, state: FSMContext) -> None:
    if not await _require_admin(message):
        await state.clear()
        return
    await state.update_data(location=message.text.strip())
    await state.set_state(AdminServerCreate.api_key)
    await message.answer("Введите API ключ сервера:")


@router.message(AdminServerCreate.api_key)
async def admin_server_api_key(message: Message, state: FSMContext) -> None:
    if not await _require_admin(message):
        await state.clear()
        return
    await state.update_data(api_key=message.text.strip())
    await state.set_state(AdminServerCreate.cost)
    await message.answer("Введите себестоимость (например 0):")


@router.message(AdminServerCreate.cost)
async def admin_server_cost(message: Message, state: FSMContext) -> None:
    if not await _require_admin(message):
        await state.clear()
        return
    try:
        cost = Decimal(message.text.replace(",", "."))
    except (ArithmeticError, ValueError):
        await message.answer("Некорректная сумма.")
        return
    if cost < 0:
        await message.answer("Сумма должна быть неотрицательной.")
        return
    data = await state.get_data()
    await state.clear()

    srv = await server_service.create(
        name=data["name"],
        ip=data["ip"],
        port=data["port"],
        host=data["host"],
        location=data["location"],
        api_key=data["api_key"],
        cost=float(cost),
    )
    await message.answer(f"Сервер {srv.name} добавлен.")
    await _send_server_list(message)
