"""Monthly product prices and plan guidance shared by the bot's screens."""

from core.services.subscription_types import PLANS as PLAN_CATALOG

from .ui import format_whole_money

PLAN_LABELS = {"basic": "BASIC", "plus": "PLUS"}
PLAN_DESCRIPTIONS = {
    "basic": (
        "Для простых задач, например Instagram, на одном устройстве. "
        "Hysteria 2, один клиент одновременно."
    ),
    "plus": (
        "Для нескольких устройств. Hysteria 2 и VLESS + REALITY, "
        "без ограничения одновременных подключений. "
        "Добавьте оба профиля: если один не подключается, выберите другой вручную."
    ),
}


def monthly_price(plan) -> str:
    return f"{format_whole_money(plan.monthly_price_rub)} ₽/месяц"


def plan_overview() -> str:
    return "\n".join(
        f"• <b>{PLAN_LABELS[plan.code]} — {monthly_price(plan)}</b>. "
        f"{PLAN_DESCRIPTIONS[plan.code]}"
        for plan in PLAN_CATALOG
        if plan.code in PLAN_LABELS
    )
