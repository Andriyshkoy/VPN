from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from core.services import TelegramActionAuditContext

from ..keyboards import (
    MENU_BALANCE,
    MENU_CANCEL,
    MENU_CONFIGS,
    MENU_INSTRUCTIONS,
    MENU_REFERRALS,
    MENU_TOP_UP,
    main_menu_keyboard,
)
from ..states import CreateConfig, RenameConfig

router = Router(name="telegram-navigation")
router.message.filter(F.chat.type == ChatType.PRIVATE)


async def _reset_state(state: FSMContext) -> None:
    await state.clear()


def _safe_flow_name(state_name: str | None) -> str:
    if state_name in {item.state for item in CreateConfig.__all_states__}:
        return "create_config"
    if state_name in {item.state for item in RenameConfig.__all_states__}:
        return "rename_config"
    return "none"


@router.message(Command("start"))
async def start_navigation(
    message: Message,
    state: FSMContext,
    command: CommandObject | None = None,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    await _reset_state(state)
    from .common import cmd_start

    await cmd_start(
        message,
        command,
        telegram_action_audit=telegram_action_audit,
    )


@router.message(Command("menu"))
async def menu_navigation(
    message: Message,
    state: FSMContext,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    await _reset_state(state)
    await message.answer(
        "Выберите нужный раздел 👇",
        reply_markup=main_menu_keyboard(),
    )
    if telegram_action_audit is not None:
        telegram_action_audit.record("navigation.menu")


@router.message(Command("help"))
async def help_navigation(
    message: Message,
    state: FSMContext,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    await _reset_state(state)
    from .common import cmd_help

    await cmd_help(
        message,
        telegram_action_audit=telegram_action_audit,
    )


@router.message(Command("balance"))
@router.message(F.text == MENU_BALANCE)
async def balance_navigation(
    message: Message,
    state: FSMContext,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    await _reset_state(state)
    from .common import cmd_balance

    await cmd_balance(
        message,
        telegram_action_audit=telegram_action_audit,
    )


@router.message(Command("history"))
async def balance_history_navigation(
    message: Message,
    state: FSMContext,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    await _reset_state(state)
    from .balance_history import cmd_balance_history

    await cmd_balance_history(
        message,
        telegram_action_audit=telegram_action_audit,
    )


@router.message(Command("configs"))
@router.message(F.text == MENU_CONFIGS)
async def configs_navigation(
    message: Message,
    state: FSMContext,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    await _reset_state(state)
    from .configs import cmd_configs

    await cmd_configs(
        message,
        telegram_action_audit=telegram_action_audit,
    )


@router.message(Command("topup"))
@router.message(F.text == MENU_TOP_UP)
async def top_up_navigation(
    message: Message,
    state: FSMContext,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    await _reset_state(state)
    from .payments import cmd_topup

    await cmd_topup(
        message,
        telegram_action_audit=telegram_action_audit,
    )


@router.message(Command("how_to_use"))
@router.message(F.text == MENU_INSTRUCTIONS)
async def instructions_navigation(
    message: Message,
    state: FSMContext,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    await _reset_state(state)
    from .common import cmd_how_to_use

    await cmd_how_to_use(
        message,
        telegram_action_audit=telegram_action_audit,
    )


@router.message(Command("referrals"))
@router.message(F.text == MENU_REFERRALS)
async def referrals_navigation(
    message: Message,
    state: FSMContext,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    await _reset_state(state)
    from .referrals import cmd_referrals

    await cmd_referrals(
        message,
        telegram_action_audit=telegram_action_audit,
    )


@router.message(Command("create_config"))
async def create_config_navigation(
    message: Message,
    state: FSMContext,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    await _reset_state(state)
    from .configs import cmd_create_config

    await cmd_create_config(
        message,
        state,
        telegram_action_audit=telegram_action_audit,
    )


@router.message(Command("cancel"))
@router.message(F.text == MENU_CANCEL)
async def cancel_navigation(
    message: Message,
    state: FSMContext,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    flow = _safe_flow_name(await state.get_state())
    await _reset_state(state)
    await message.answer(
        "Действие отменено.",
        reply_markup=main_menu_keyboard(),
    )
    if telegram_action_audit is not None:
        telegram_action_audit.record(
            "navigation.cancel",
            metadata={"flow": flow},
        )
