from __future__ import annotations

import html
import os
import tempfile
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)

from core.config import settings
from core.domain import VPNOperationStatus, VPNState
from core.exceptions import APIConnectionError, InsufficientBalanceError, ServiceError

from ..keyboards import cancel_keyboard, main_menu_keyboard
from ..states import CreateConfig, RenameConfig
from ..ui import (
    format_billing_interval,
    format_money,
    safe_callback_answer,
    safe_document_filename,
    safe_edit_text,
)
from .base import (
    billing_service,
    config_service,
    get_or_create_user,
    router,
    server_service,
)

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


def _config_states(config: Any) -> tuple[str, str]:
    """Return control-plane states while supporting pre-refactor test doubles."""

    legacy_state = (
        VPNState.SUSPENDED.value
        if getattr(config, "suspended", False)
        else VPNState.ACTIVE.value
    )
    desired = getattr(config, "desired_state", legacy_state) or legacy_state
    actual = getattr(config, "actual_state", legacy_state) or legacy_state
    return str(desired), str(actual)


def _config_status(config: Any) -> tuple[str, str]:
    desired, actual = _config_states(config)
    operation_status = getattr(config, "operation_status", None)
    if operation_status in {
        VPNOperationStatus.REJECTED.value,
        VPNOperationStatus.EXHAUSTED.value,
    }:
        return "⚠️", "требуется проверка"
    if desired == VPNState.REVOKED.value:
        return "🗑", "удаляется"
    if actual == VPNState.PROVISIONING.value:
        if operation_status is None and getattr(config, "last_error", None):
            return "⚠️", "требуется проверка"
        if operation_status == VPNOperationStatus.FAILED.value:
            return "⏳", "повторяем создание"
        return "⏳", "создаётся"
    if actual == VPNState.FAILED.value:
        return "⚠️", "требуется проверка"
    if desired != actual:
        if desired == VPNState.ACTIVE.value:
            return "⏳", "возобновляется"
        if desired == VPNState.SUSPENDED.value:
            return "⏳", "приостанавливается"
        return "⏳", "обновляется"
    if actual == VPNState.ACTIVE.value:
        return "🟢", "активна"
    if actual == VPNState.SUSPENDED.value:
        return "⏸", "приостановлена"
    return "⚠️", "требуется проверка"


def _button_title(config: Any) -> str:
    status, _ = _config_status(config)
    title = " ".join(str(config.display_name).split()) or "Без названия"
    return f"{status} {title[:48]}"


def _configs_markup(configs: list[Any]) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                text="➕ Создать конфигурацию",
                callback_data="cfg:create",
            )
        ]
    ]
    buttons.extend(
        [
            InlineKeyboardButton(
                text=_button_title(config), callback_data=f"cfg:{config.id}"
            )
        ]
        for config in configs
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _configs_view(
    tg_id: int,
    username: str | None,
) -> tuple[str, InlineKeyboardMarkup]:
    user = await get_or_create_user(tg_id, username)
    all_configs = await config_service.list(owner_id=user.id)
    configs = [
        config
        for config in all_configs
        if _config_states(config) != (VPNState.REVOKED.value, VPNState.REVOKED.value)
    ]
    if configs:
        text = (
            "🗂 <b>Мои конфигурации</b>\n\n"
            "🟢 активна · ⏸ на паузе · ⏳ обрабатывается · ⚠️ нужна проверка\n"
            "Выберите конфигурацию или создайте новую."
        )
    else:
        text = (
            "🗂 <b>Мои конфигурации</b>\n\n"
            "У вас пока нет конфигураций. Создайте первую — бот сразу пришлёт "
            "готовый <code>.ovpn</code>-файл."
        )
    return text, _configs_markup(configs)


async def cmd_configs(message: Message) -> None:
    text, markup = await _configs_view(
        message.from_user.id,
        message.from_user.username,
    )
    await message.answer(text, reply_markup=markup)


async def _begin_create_config(
    target: Message,
    state: FSMContext,
    *,
    tg_id: int,
    username: str | None,
) -> None:
    await get_or_create_user(tg_id, username)
    if settings.maintenance_mode or not settings.provisioning_enabled:
        await target.answer(
            "Создание конфигураций временно недоступно. Попробуйте позже.",
            reply_markup=main_menu_keyboard(),
        )
        return

    servers = await server_service.list()
    if not servers:
        await target.answer(
            "Сейчас нет доступных серверов. Попробуйте позже.",
            reply_markup=main_menu_keyboard(),
        )
        return

    buttons = [
        [
            InlineKeyboardButton(
                text=" ".join([server.location, server.name]),
                callback_data=f"server:{server.id}",
            )
        ]
        for server in servers
    ]
    await target.answer(
        "➕ <b>Новая конфигурация</b>\n\n"
        "Выберите ближайший сервер.\n\n"
        f"Создание — <b>{format_money(settings.config_creation_cost)} ₽</b>.\n"
        f"Обслуживание — <b>{format_money(settings.per_config_cost)} ₽</b> "
        f"{format_billing_interval(settings.billing_interval)} за каждую "
        "конфигурацию.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await state.set_state(CreateConfig.choosing_server)


async def cmd_create_config(message: Message, state: FSMContext) -> None:
    await _begin_create_config(
        message,
        state,
        tg_id=message.from_user.id,
        username=message.from_user.username,
    )


@router.callback_query(F.data == "cfg:create")
async def create_config_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _begin_create_config(
        callback.message,
        state,
        tg_id=callback.from_user.id,
        username=callback.from_user.username,
    )
    await safe_callback_answer(callback)


@router.callback_query(F.data == "cfg:list")
async def list_configs_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, markup = await _configs_view(
        callback.from_user.id,
        callback.from_user.username,
    )
    await safe_edit_text(callback.message, text, reply_markup=markup)
    await safe_callback_answer(callback)


async def _callback_id(
    callback: CallbackQuery,
    prefix: str,
) -> int | None:
    try:
        raw = callback.data.removeprefix(prefix)
        value = int(raw)
        if value <= 0 or callback.data != f"{prefix}{value}":
            raise ValueError
        return value
    except (AttributeError, TypeError, ValueError):
        await safe_callback_answer(
            callback,
            "Кнопка устарела. Откройте раздел заново.",
            show_alert=True,
        )
        return None


@router.callback_query(F.data.startswith("server:"))
async def choose_server(callback: CallbackQuery, state: FSMContext) -> None:
    server_id = await _callback_id(callback, "server:")
    if server_id is None:
        return
    server = await server_service.get(server_id)
    if server is None:
        await safe_callback_answer(
            callback,
            "Сервер больше недоступен. Выберите другой.",
            show_alert=True,
        )
        return

    await state.update_data(server_id=server_id)
    await callback.message.answer(
        "📝 <b>Введите название конфигурации</b>\n\n"
        "Например: <i>Мой телефон</i> или <i>Ноутбук</i>. Это название будет "
        "видно только вам, его можно изменить позже.",
        reply_markup=cancel_keyboard(),
    )
    await state.set_state(CreateConfig.entering_name)
    await safe_callback_answer(callback)


@router.message(CreateConfig.choosing_server, ~F.successful_payment)
async def choose_server_message_hint(message: Message) -> None:
    await message.answer(
        "Выберите сервер кнопкой в сообщении выше. Чтобы выйти, откройте "
        "другой раздел в меню.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(CreateConfig.entering_name, F.text, ~F.successful_payment)
async def got_name(
    message: Message,
    state: FSMContext,
    bot: Any,
    event_update: Update,
) -> None:
    data = await state.get_data()
    server_id = data.get("server_id")
    if not server_id:
        await message.answer(
            "Сервер не выбран. Начните создание заново.",
            reply_markup=main_menu_keyboard(),
        )
        await state.clear()
        return

    display_name = (message.text or "").strip()
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    purchase_key = f"telegram:create-config:update:{event_update.update_id}"
    unique_name = uuid5(NAMESPACE_URL, purchase_key).hex
    try:
        config = await billing_service.create_paid_config(
            server_id=server_id,
            owner_id=user.id,
            name=unique_name,
            display_name=display_name,
            creation_cost=settings.config_creation_cost,
            idempotency_key=purchase_key,
        )
    except InsufficientBalanceError:
        await message.answer(
            "Недостаточно средств. Сначала пополните баланс.",
            reply_markup=main_menu_keyboard(),
        )
        await state.clear()
        return
    except APIConnectionError:
        # The config/reservation intent is already durable. Let the inbox
        # retry this exact update without charging or provisioning twice.
        raise
    except ServiceError:
        await message.answer(
            "Не удалось создать конфигурацию. Проверьте название и попробуйте "
            "ещё раз позже.",
            reply_markup=main_menu_keyboard(),
        )
        await state.clear()
        return

    # Delivery belongs to the durable update attempt. Any Manager or Telegram
    # failure must bubble up so this update is retried.
    content = await config_service.download_config(config.id)
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        await bot.send_document(
            message.from_user.id,
            FSInputFile(tmp_path, filename=safe_document_filename(display_name)),
        )
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    await message.answer(
        "✅ Конфигурация создана и готова к импорту.\n\n"
        "Откройте <b>«Инструкции»</b>, если подключаете устройство впервые.",
        reply_markup=main_menu_keyboard(),
    )
    await state.clear()


@router.message(CreateConfig.entering_name, ~F.successful_payment)
async def config_name_message_hint(message: Message) -> None:
    await message.answer(
        "Название нужно отправить текстом, например «Мой телефон».",
        reply_markup=cancel_keyboard(),
    )


async def _owned_config(
    callback: CallbackQuery,
    prefix: str,
) -> tuple[Any, Any] | None:
    config_id = await _callback_id(callback, prefix)
    if config_id is None:
        return None
    user = await get_or_create_user(
        callback.from_user.id,
        callback.from_user.username,
    )
    config = await config_service.get(config_id)
    if not config or config.owner_id != user.id:
        await safe_callback_answer(
            callback,
            "Конфигурация не найдена.",
            show_alert=True,
        )
        return None
    return config, user


async def _config_details(config: Any) -> tuple[str, InlineKeyboardMarkup]:
    server = await server_service.get(config.server_id)
    server_name = (
        f"{html.escape(server.name)} ({html.escape(server.location)})"
        if server
        else "сервер недоступен"
    )
    desired, actual = _config_states(config)
    status_icon, status_text = _config_status(config)
    can_download = actual in {
        VPNState.ACTIVE.value,
        VPNState.SUSPENDED.value,
    }
    is_being_deleted = desired == VPNState.REVOKED.value

    buttons: list[list[InlineKeyboardButton]] = []
    if can_download and not is_being_deleted:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="⬇️ Скачать .ovpn", callback_data=f"dl:{config.id}"
                )
            ]
        )
    if not is_being_deleted:
        buttons.extend(
            [
                [
                    InlineKeyboardButton(
                        text="✏️ Переименовать", callback_data=f"rn:{config.id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🗑 Удалить", callback_data=f"del:{config.id}"
                    )
                ],
            ]
        )
    buttons.append([InlineKeyboardButton(text="⬅️ К списку", callback_data="cfg:list")])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    status_note = ""
    operation_status = getattr(config, "operation_status", None)
    if operation_status in {
        VPNOperationStatus.REJECTED.value,
        VPNOperationStatus.EXHAUSTED.value,
    } or (
        actual == VPNState.PROVISIONING.value
        and operation_status is None
        and getattr(config, "last_error", None)
    ):
        status_note = (
            "\n\nАвтоматическая обработка не завершилась. Мы сохранили "
            "конфигурацию — обратитесь в поддержку, списывать её повторно не нужно."
        )
    elif actual == VPNState.PROVISIONING.value:
        if operation_status == VPNOperationStatus.FAILED.value:
            status_note = (
                "\n\nСвязь с сервером временно прервалась. Повторная попытка "
                "выполнится автоматически."
            )
        else:
            status_note = "\n\nПодождите немного и откройте раздел ещё раз."
    elif actual == VPNState.FAILED.value:
        status_note = (
            "\n\nАвтоматическая обработка не завершилась. Мы сохранили "
            "конфигурацию — обратитесь в поддержку, списывать её повторно не нужно."
        )
    elif desired != actual:
        status_note = "\n\nИзменение уже принято. Обновите этот экран чуть позже."
    text = (
        f"🗂 <b>{html.escape(config.display_name)}</b>\n\n"
        f"Сервер: {server_name}\n"
        f"Статус: {status_icon} {status_text}"
        f"{status_note}"
    )
    return text, markup


@router.callback_query(F.data.startswith("cfg:"))
async def show_config(callback: CallbackQuery) -> None:
    owned = await _owned_config(callback, "cfg:")
    if owned is None:
        return
    config, _ = owned
    text, markup = await _config_details(config)
    await safe_edit_text(callback.message, text, reply_markup=markup)
    await safe_callback_answer(callback)


@router.callback_query(F.data.startswith("sus:"))
async def suspend_config_cb(callback: CallbackQuery) -> None:
    owned = await _owned_config(callback, "sus:")
    if owned is None:
        return
    await safe_callback_answer(
        callback,
        "Ручная пауза временно недоступна. Для потерянного устройства удалите "
        "конфигурацию.",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("uns:"))
async def unsuspend_config_cb(callback: CallbackQuery) -> None:
    owned = await _owned_config(callback, "uns:")
    if owned is None:
        return
    await safe_callback_answer(
        callback,
        "Ручное возобновление временно недоступно. Конфигурации, остановленные "
        "из-за баланса, включатся после пополнения.",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("del:"))
async def delete_config_cb(callback: CallbackQuery) -> None:
    owned = await _owned_config(callback, "del:")
    if owned is None:
        return
    config, _ = owned
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, удалить безвозвратно",
                    callback_data=f"del_ok:{config.id}",
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data=f"cfg:{config.id}")],
        ]
    )
    await safe_edit_text(
        callback.message,
        "🗑 <b>Удалить конфигурацию?</b>\n\n"
        f"<b>{html.escape(config.display_name)}</b> перестанет подключаться. "
        "Отменить удаление после подтверждения нельзя.",
        reply_markup=markup,
    )
    await safe_callback_answer(callback)


@router.callback_query(F.data.startswith("del_ok:"))
async def confirm_delete_config_cb(callback: CallbackQuery) -> None:
    owned = await _owned_config(callback, "del_ok:")
    if owned is None:
        return
    config, _ = owned
    try:
        await config_service.revoke_config(config.id)
    except ServiceError:
        await callback.message.answer("Не удалось удалить конфигурацию.")
        await safe_callback_answer(callback)
        return

    text, markup = await _configs_view(
        callback.from_user.id,
        callback.from_user.username,
    )
    await safe_edit_text(
        callback.message,
        "✅ Конфигурация удалена.\n\n" + text,
        reply_markup=markup,
    )
    await safe_callback_answer(callback)


@router.callback_query(F.data.startswith("dl:"))
async def download_config_cb(callback: CallbackQuery, bot: Any) -> None:
    owned = await _owned_config(callback, "dl:")
    if owned is None:
        return
    config, _ = owned
    try:
        content = await config_service.download_config(config.id)
    except ServiceError:
        await callback.message.answer("Не удалось скачать конфигурацию.")
        await safe_callback_answer(callback)
        return

    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        await bot.send_document(
            callback.from_user.id,
            FSInputFile(
                tmp_path,
                filename=safe_document_filename(config.display_name),
            ),
        )
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    await safe_callback_answer(callback)


@router.callback_query(F.data.startswith("rn:"))
async def rename_config_cb(callback: CallbackQuery, state: FSMContext) -> None:
    owned = await _owned_config(callback, "rn:")
    if owned is None:
        return
    config, _ = owned
    await state.update_data(config_id=config.id)
    await callback.message.answer(
        "Введите новое название конфигурации:",
        reply_markup=cancel_keyboard(),
    )
    await state.set_state(RenameConfig.entering_name)
    await safe_callback_answer(callback)


@router.message(RenameConfig.entering_name, F.text, ~F.successful_payment)
async def got_new_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    config_id = data.get("config_id")
    if not config_id:
        await message.answer(
            "Конфигурация не выбрана. Начните заново.",
            reply_markup=main_menu_keyboard(),
        )
        await state.clear()
        return

    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    config = await config_service.get(config_id)
    if not config or config.owner_id != user.id:
        await message.answer(
            "Конфигурация не найдена.",
            reply_markup=main_menu_keyboard(),
        )
        await state.clear()
        return
    try:
        await config_service.rename_config(config_id, message.text or "")
        await message.answer(
            "✅ Конфигурация переименована.",
            reply_markup=main_menu_keyboard(),
        )
    except ServiceError:
        await message.answer(
            "Не удалось переименовать конфигурацию. Название должно содержать "
            "от 1 до 128 символов.",
            reply_markup=main_menu_keyboard(),
        )
    await state.clear()


@router.message(RenameConfig.entering_name, ~F.successful_payment)
async def rename_message_hint(message: Message) -> None:
    await message.answer(
        "Новое название нужно отправить текстом.",
        reply_markup=cancel_keyboard(),
    )
