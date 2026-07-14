from __future__ import annotations

from decimal import Decimal
from html import escape
from urllib.parse import quote

from aiogram import Bot, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.deep_linking import create_start_link

from core.config import settings
from core.db.unit_of_work import uow
from core.services import (
    ReferralOverview,
    ReferralService,
    TelegramActionAuditContext,
)

from ..keyboards import referral_program_keyboard
from ..ui import format_money, safe_callback_answer, safe_edit_text
from .base import get_or_create_user, router

__all__ = ["cmd_referrals", "legacy_referrals_callback"]

referral_service = ReferralService(uow)


def _format_percent(rate_bps: int) -> str:
    value = Decimal(rate_bps) / Decimal(100)
    return f"{value.normalize():f}".replace(".", ",")


def render_referral_program(overview: ReferralOverview, invite_link: str) -> str:
    level_1_rate = _format_percent(settings.referral_level_1_rate_bps)
    level_2_rate = _format_percent(settings.referral_level_2_rate_bps)
    status = (
        "Приглашайте друзей и получайте VPN-бонусы с их пополнений:\n"
        if settings.referral_rewards_enabled
        else (
            "⏸ <b>Новые начисления временно приостановлены.</b> "
            "Ссылка и уже начисленные бонусы продолжают работать.\n\n"
            "Условия после возобновления:\n"
        )
    )
    return (
        "🎁 <b>Реферальная программа</b>\n\n"
        f"{status}"
        f"• <b>{level_1_rate}%</b> — с каждого пополнения приглашённого вами "
        "пользователя;\n"
        f"• <b>{level_2_rate}%</b> — с пополнений пользователей второго уровня.\n\n"
        "Бонус сразу поступает на внутренний баланс и расходуется на VPN. "
        "Вывести его деньгами нельзя.\n\n"
        "<b>Ваша статистика</b>\n"
        f"Первый уровень: <b>{overview.level_1_count}</b>\n"
        f"Второй уровень: <b>{overview.level_2_count}</b>\n"
        f"Начислено за первый уровень: "
        f"<b>{format_money(overview.level_1_earned)} ₽</b>\n"
        f"Начислено за второй уровень: "
        f"<b>{format_money(overview.level_2_earned)} ₽</b>\n"
        f"Всего заработано: <b>{format_money(overview.total_earned)} ₽</b>\n\n"
        "<b>Ваша пригласительная ссылка</b>\n"
        f"<code>{escape(invite_link)}</code>"
    )


async def _referral_screen(
    *,
    tg_id: int,
    username: str | None,
    bot: Bot,
) -> tuple[str, InlineKeyboardMarkup] | None:
    user = await get_or_create_user(tg_id, username)
    if user is None:
        return None
    overview = await referral_service.overview(user.id)
    invite_link = await create_start_link(
        bot,
        f"ref_{overview.referral_code}",
        encode=False,
    )
    share_url = (
        "https://t.me/share/url?"
        f"url={quote(invite_link, safe='')}"
        "&text="
        f"{quote('Подключайся к моему VPN по приглашению 👇', safe='')}"
    )
    return (
        render_referral_program(overview, invite_link),
        referral_program_keyboard(share_url),
    )


def _record_referral_outcome(
    telegram_action_audit: TelegramActionAuditContext | None,
) -> None:
    if telegram_action_audit is None:
        return
    if settings.referral_rewards_enabled:
        telegram_action_audit.record("referral.overview")
    else:
        telegram_action_audit.record(
            "referral.overview",
            result="unavailable",
            metadata={"reason_code": "referral_rewards_disabled"},
        )


async def cmd_referrals(
    message: Message,
    bot: Bot | None = None,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    screen = await _referral_screen(
        tg_id=message.from_user.id,
        username=message.from_user.username,
        bot=bot or message.bot,
    )
    if screen is None:
        if telegram_action_audit is not None:
            telegram_action_audit.record(
                "referral.overview",
                result="unavailable",
                metadata={"reason_code": "account_unavailable"},
            )
        return
    text, markup = screen
    await message.answer(
        text,
        reply_markup=markup,
        disable_web_page_preview=True,
    )
    _record_referral_outcome(telegram_action_audit)


@router.callback_query(F.data.startswith("refs:"))
async def legacy_referrals_callback(
    callback: CallbackQuery,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    """Keep already-sent referral buttons useful after the rollout."""

    screen = await _referral_screen(
        tg_id=callback.from_user.id,
        username=callback.from_user.username,
        bot=callback.bot,
    )
    if screen is None:
        await safe_callback_answer(callback)
        if telegram_action_audit is not None:
            telegram_action_audit.record(
                "referral.overview",
                result="unavailable",
                metadata={"reason_code": "account_unavailable"},
            )
        return
    text, markup = screen
    await safe_edit_text(
        callback.message,
        text,
        reply_markup=markup,
        disable_web_page_preview=True,
    )
    await safe_callback_answer(callback)
    _record_referral_outcome(telegram_action_audit)
