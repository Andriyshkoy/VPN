from __future__ import annotations

import html
import os
import tempfile
from typing import Any

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
from core.exceptions import ServiceError
from core.services.telegram_user_actions import TelegramActionAuditContext

from ..keyboards import cancel_keyboard, main_menu_keyboard
from ..states import CreateConfig, RenameConfig
from ..ui import (
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


async def cmd_configs(
    message: Message,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    text, markup = await _configs_view(
        message.from_user.id,
        message.from_user.username,
    )
    await message.answer(text, reply_markup=markup)
    if telegram_action_audit is not None:
        telegram_action_audit.record("vpn.config_list")


async def _begin_create_config(
    target: Message,
    state: FSMContext,
    *,
    tg_id: int,
    username: str | None,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    # Retire stale OpenVPN purchase flows without issuing or charging.
    from .subscriptions import cmd_subscription

    await state.clear()
    await cmd_subscription(
        target,
        telegram_action_audit=telegram_action_audit,
        tg_id=tg_id,
        username=username,
    )


async def cmd_create_config(
    message: Message,
    state: FSMContext,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    await _begin_create_config(
        message,
        state,
        tg_id=message.from_user.id,
        username=message.from_user.username,
        telegram_action_audit=telegram_action_audit,
    )


@router.callback_query(F.data == "cfg:create")
async def create_config_cb(
    callback: CallbackQuery,
    state: FSMContext,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    await state.clear()
    await _begin_create_config(
        callback.message,
        state,
        tg_id=callback.from_user.id,
        username=callback.from_user.username,
        telegram_action_audit=telegram_action_audit,
    )
    await safe_callback_answer(callback)


@router.callback_query(F.data == "cfg:list")
async def list_configs_cb(
    callback: CallbackQuery,
    state: FSMContext,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    await state.clear()
    text, markup = await _configs_view(
        callback.from_user.id,
        callback.from_user.username,
    )
    await safe_edit_text(callback.message, text, reply_markup=markup)
    await safe_callback_answer(callback)
    if telegram_action_audit is not None:
        telegram_action_audit.record("vpn.config_list")


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
async def choose_server(
    callback: CallbackQuery,
    state: FSMContext,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    await _begin_create_config(
        callback.message,
        state,
        tg_id=callback.from_user.id,
        username=callback.from_user.username,
        telegram_action_audit=telegram_action_audit,
    )
    await safe_callback_answer(callback)


@router.message(CreateConfig.choosing_server, ~F.successful_payment)
async def choose_server_message_hint(
    message: Message,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    await message.answer(
        "Выберите сервер кнопкой в сообщении выше. Чтобы выйти, откройте "
        "другой раздел в меню.",
        reply_markup=main_menu_keyboard(),
    )
    if telegram_action_audit is not None:
        telegram_action_audit.record(
            "vpn.config_server_select",
            result="invalid",
            metadata={"reason_code": "button_required"},
        )


@router.message(CreateConfig.entering_name, F.text, ~F.successful_payment)
async def got_name(
    message: Message,
    state: FSMContext,
    bot: Any,
    event_update: Update,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    await _begin_create_config(
        message,
        state,
        tg_id=message.from_user.id,
        username=message.from_user.username,
        telegram_action_audit=telegram_action_audit,
    )


@router.message(CreateConfig.entering_name, ~F.successful_payment)
async def config_name_message_hint(
    message: Message,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    await message.answer(
        "Название нужно отправить текстом, например «Мой телефон».",
        reply_markup=cancel_keyboard(),
    )
    if telegram_action_audit is not None:
        telegram_action_audit.record(
            "vpn.config_create_submit",
            result="invalid",
            metadata={"reason_code": "text_required"},
        )


async def _owned_config(
    callback: CallbackQuery,
    prefix: str,
    *,
    telegram_action_audit: TelegramActionAuditContext | None = None,
    audit_action: str,
) -> tuple[Any, Any] | None:
    config_id = await _callback_id(callback, prefix)
    if config_id is None:
        if telegram_action_audit is not None:
            telegram_action_audit.record(
                audit_action,
                result="invalid",
                metadata={"reason_code": "invalid_callback"},
            )
        return None
    user = await get_or_create_user(
        callback.from_user.id,
        callback.from_user.username,
    )
    config = await config_service.get(config_id)
    if not config or config.owner_id != user.id:
        if telegram_action_audit is not None:
            # Never persist the attacker-controlled target before ownership is
            # proven. The public response deliberately has the same shape for
            # a missing and a foreign configuration.
            telegram_action_audit.record(
                audit_action,
                result="rejected",
                metadata={"reason_code": "config_not_found"},
            )
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
async def show_config(
    callback: CallbackQuery,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    owned = await _owned_config(
        callback,
        "cfg:",
        telegram_action_audit=telegram_action_audit,
        audit_action="vpn.config_view",
    )
    if owned is None:
        return
    config, _ = owned
    text, markup = await _config_details(config)
    await safe_edit_text(callback.message, text, reply_markup=markup)
    await safe_callback_answer(callback)
    if telegram_action_audit is not None:
        telegram_action_audit.record(
            "vpn.config_view",
            metadata={"config_id": config.id},
        )


@router.callback_query(F.data.startswith("sus:"))
async def suspend_config_cb(
    callback: CallbackQuery,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    owned = await _owned_config(
        callback,
        "sus:",
        telegram_action_audit=telegram_action_audit,
        audit_action="vpn.config_suspend",
    )
    if owned is None:
        return
    config, _ = owned
    if telegram_action_audit is not None:
        telegram_action_audit.record(
            "vpn.config_suspend",
            result="unavailable",
            metadata={
                "config_id": config.id,
                "reason_code": "manual_pause_disabled",
            },
        )
    await safe_callback_answer(
        callback,
        "Ручная пауза временно недоступна. Для потерянного устройства удалите "
        "конфигурацию.",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("uns:"))
async def unsuspend_config_cb(
    callback: CallbackQuery,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    owned = await _owned_config(
        callback,
        "uns:",
        telegram_action_audit=telegram_action_audit,
        audit_action="vpn.config_resume",
    )
    if owned is None:
        return
    config, _ = owned
    if telegram_action_audit is not None:
        telegram_action_audit.record(
            "vpn.config_resume",
            result="unavailable",
            metadata={
                "config_id": config.id,
                "reason_code": "manual_resume_disabled",
            },
        )
    await safe_callback_answer(
        callback,
        "Ручное возобновление временно недоступно. Конфигурации, остановленные "
        "из-за баланса, включатся после пополнения.",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("del:"))
async def delete_config_cb(
    callback: CallbackQuery,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    owned = await _owned_config(
        callback,
        "del:",
        telegram_action_audit=telegram_action_audit,
        audit_action="vpn.config_delete_request",
    )
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
    if telegram_action_audit is not None:
        telegram_action_audit.record(
            "vpn.config_delete_request",
            metadata={"config_id": config.id},
        )


@router.callback_query(F.data.startswith("del_ok:"))
async def confirm_delete_config_cb(
    callback: CallbackQuery,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    owned = await _owned_config(
        callback,
        "del_ok:",
        telegram_action_audit=telegram_action_audit,
        audit_action="vpn.config_delete_confirm",
    )
    if owned is None:
        return
    config, _ = owned
    try:
        await config_service.revoke_config(config.id)
    except ServiceError:
        if telegram_action_audit is not None:
            telegram_action_audit.record(
                "vpn.config_delete_confirm",
                result="failed",
                metadata={
                    "config_id": config.id,
                    "reason_code": "vpn_operation_failed",
                },
            )
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
    if telegram_action_audit is not None:
        telegram_action_audit.record(
            "vpn.config_delete_confirm",
            metadata={"config_id": config.id},
        )


@router.callback_query(F.data.startswith("dl:"))
async def download_config_cb(
    callback: CallbackQuery,
    bot: Any,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    owned = await _owned_config(
        callback,
        "dl:",
        telegram_action_audit=telegram_action_audit,
        audit_action="vpn.config_download",
    )
    if owned is None:
        return
    config, _ = owned
    if telegram_action_audit is not None:
        telegram_action_audit.record(
            "vpn.config_download",
            result="handled",
            metadata={"config_id": config.id},
        )
    try:
        content = await config_service.download_config(config.id)
    except ServiceError:
        if telegram_action_audit is not None:
            telegram_action_audit.record(
                "vpn.config_download",
                result="failed",
                metadata={
                    "config_id": config.id,
                    "reason_code": "download_failed",
                },
            )
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
    if telegram_action_audit is not None:
        telegram_action_audit.record(
            "vpn.config_download",
            metadata={"config_id": config.id},
        )


@router.callback_query(F.data.startswith("rn:"))
async def rename_config_cb(
    callback: CallbackQuery,
    state: FSMContext,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    owned = await _owned_config(
        callback,
        "rn:",
        telegram_action_audit=telegram_action_audit,
        audit_action="vpn.config_rename_start",
    )
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
    if telegram_action_audit is not None:
        telegram_action_audit.record(
            "vpn.config_rename_start",
            metadata={"config_id": config.id},
        )


@router.message(RenameConfig.entering_name, F.text, ~F.successful_payment)
async def got_new_name(
    message: Message,
    state: FSMContext,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    data = await state.get_data()
    config_id = data.get("config_id")
    if not config_id:
        if telegram_action_audit is not None:
            telegram_action_audit.record(
                "vpn.config_rename_submit",
                result="rejected",
                metadata={"reason_code": "config_not_selected"},
            )
        await message.answer(
            "Конфигурация не выбрана. Начните заново.",
            reply_markup=main_menu_keyboard(),
        )
        await state.clear()
        return

    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    config = await config_service.get(config_id)
    if not config or config.owner_id != user.id:
        if telegram_action_audit is not None:
            telegram_action_audit.record(
                "vpn.config_rename_submit",
                result="rejected",
                metadata={"reason_code": "config_not_found"},
            )
        await message.answer(
            "Конфигурация не найдена.",
            reply_markup=main_menu_keyboard(),
        )
        await state.clear()
        return
    if telegram_action_audit is not None:
        telegram_action_audit.record(
            "vpn.config_rename_submit",
            result="handled",
            metadata={"config_id": config.id},
        )
    try:
        await config_service.rename_config(config_id, message.text or "")
        await message.answer(
            "✅ Конфигурация переименована.",
            reply_markup=main_menu_keyboard(),
        )
        if telegram_action_audit is not None:
            telegram_action_audit.record(
                "vpn.config_rename_submit",
                metadata={"config_id": config.id},
            )
    except ServiceError:
        if telegram_action_audit is not None:
            telegram_action_audit.record(
                "vpn.config_rename_submit",
                result="invalid",
                metadata={
                    "config_id": config.id,
                    "reason_code": "invalid_name",
                },
            )
        await message.answer(
            "Не удалось переименовать конфигурацию. Название должно содержать "
            "от 1 до 128 символов.",
            reply_markup=main_menu_keyboard(),
        )
    await state.clear()


@router.message(RenameConfig.entering_name, ~F.successful_payment)
async def rename_message_hint(
    message: Message,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    await message.answer(
        "Новое название нужно отправить текстом.",
        reply_markup=cancel_keyboard(),
    )
    if telegram_action_audit is not None:
        telegram_action_audit.record(
            "vpn.config_rename_submit",
            result="invalid",
            metadata={"reason_code": "text_required"},
        )
