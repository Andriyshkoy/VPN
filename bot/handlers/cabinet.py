from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from aiogram import F
from aiogram.types import CallbackQuery, Message

from .base import billing_service, config_service, is_admin, require_user, router, user_service
from .keyboards import cabinet_kb, transactions_filters_kb

TX_DAYS_PER_PAGE = 7
FORECAST_WINDOW_DAYS = 7

KIND_LABELS: dict[str, str] = {
    "topup": "Пополнение",
    "usage": "Использование",
    "config_creation": "Создание конфига",
    "withdraw": "Списание",
    "referral_bonus": "Бонус реферала",
}

FILTERS: dict[str, dict[str, object | None]] = {
    "all": {"kinds": None, "amount_sign": None},
    "topup": {"kinds": ["topup"], "amount_sign": None},
    "expense": {"kinds": None, "amount_sign": "negative"},
    "bonus": {"kinds": ["referral_bonus"], "amount_sign": None},
}


def _format_money(amount: Decimal) -> str:
    return f"{amount:.2f}"


def _summarize_expenses(txs) -> list[tuple[str, Decimal, int]]:
    summary: dict[str, dict[str, object]] = {}
    for tx in txs:
        if tx.amount >= 0:
            continue
        data = summary.setdefault(
            tx.kind, {"total": Decimal("0.00"), "count": 0}
        )
        data["total"] = data["total"] + (-tx.amount)
        data["count"] = data["count"] + 1
    ordered = ["usage", "config_creation", "withdraw"]
    items: list[tuple[str, Decimal, int]] = []
    for kind in ordered:
        if kind in summary:
            data = summary[kind]
            items.append((kind, data["total"], data["count"]))
    for kind in sorted(k for k in summary.keys() if k not in ordered):
        data = summary[kind]
        items.append((kind, data["total"], data["count"]))
    return items


def _forecast_text(
    *,
    balance: Decimal,
    avg_daily_spend: Decimal,
    now: datetime,
    label: str,
) -> str:
    if balance <= 0:
        return "🔮 Прогноз: баланс уже исчерпан."
    if avg_daily_spend <= 0:
        return "🔮 Прогноз: нет данных о текущих расходах."
    days_left = float(balance / avg_daily_spend)
    if days_left <= 0:
        return "🔮 Прогноз: баланс уже исчерпан."
    eta = now + timedelta(days=days_left)
    if days_left >= 1:
        days = int(days_left)
        hours = int((days_left - days) * 24)
        return (
            f"🔮 Прогноз: ~{days} дн. {hours} ч. ({label}), "
            f"до {eta:%d.%m.%Y %H:%M}"
        )
    hours_left = max(1, int(days_left * 24))
    return f"🔮 Прогноз: ~{hours_left} ч. ({label}), до {eta:%d.%m.%Y %H:%M}"


async def _send_cabinet(target: Message | CallbackQuery, user) -> None:
    active_count = await config_service.count_active(user.id)
    suspended = await config_service.list_suspended(owner_id=user.id)
    settings = await billing_service.get_settings()
    text = (
        "👤 <b>Личный кабинет</b>\n\n"
        f"💰 Баланс: <b>{_format_money(user.balance)} ₽</b>\n"
        f"✅ Активные конфиги: <b>{active_count}</b>\n"
        f"⏸ Приостановленные: <b>{len(suspended)}</b>\n\n"
        "<b>Тарифы:</b>\n"
        f"• создание конфига — {settings.config_creation_cost} ₽\n"
        f"• использование — {settings.monthly_config_cost} ₽ / месяц\n"
    )
    send_method = target.answer if isinstance(target, Message) else target.message.edit_text
    await send_method(text, reply_markup=cabinet_kb(), parse_mode="HTML")
    if isinstance(target, CallbackQuery):
        await target.answer()


@router.message(F.text == "👤 Личный кабинет")
async def cabinet_message(message: Message) -> None:
    user = await require_user(message)
    if not user:
        return
    await _send_cabinet(message, user)


@router.callback_query(F.data == "cabinet:home")
async def cabinet_callback(callback: CallbackQuery) -> None:
    user = await require_user(callback)
    if not user:
        return
    await _send_cabinet(callback, user)


def _build_tx_summary(
    txs,
    *,
    start_date,
    end_date,
    user_label: str | None = None,
    expense_overview: list[tuple[str, Decimal, int]] | None = None,
    forecast_line: str | None = None,
) -> str:
    grouped: dict[object, dict[str, object]] = {}
    for tx in txs:
        day = tx.created_at.date()
        entry = grouped.setdefault(day, {"incomes": [], "expenses": {}})
        if tx.amount < 0:
            expenses = entry["expenses"]
            summary = expenses.setdefault(
                tx.kind, {"total": Decimal("0.00"), "count": 0}
            )
            summary["total"] += -tx.amount
            summary["count"] += 1
        else:
            entry["incomes"].append(tx)

    header = "📑 <b>Детализация счета</b>"
    if user_label:
        header = f"📑 <b>Детализация счета</b>\n<b>{user_label}</b>"

    lines = [header, f"Период: {start_date:%d.%m.%Y} — {end_date:%d.%m.%Y}"]
    if forecast_line:
        lines.append(forecast_line)

    if expense_overview is not None:
        lines.append("\n<b>Расходы за период:</b>")
        if not expense_overview:
            lines.append("• нет списаний")
        for kind, total, count in expense_overview:
            label = KIND_LABELS.get(kind, kind)
            count_suffix = f" ×{count}" if count > 1 else ""
            lines.append(f"• {label}: {_format_money(total)} ₽{count_suffix}")

    if not grouped:
        lines.append("\nНет операций за этот период.")
        return "\n".join(lines)

    for day in sorted(grouped.keys(), reverse=True):
        entry = grouped[day]
        lines.append(f"\n📅 <b>{day:%d.%m.%Y}</b>")

        incomes = entry["incomes"]
        if incomes:
            lines.append("Пополнения и бонусы:")
            for tx in incomes:
                label = KIND_LABELS.get(tx.kind, tx.kind)
                suffix = f" ({tx.source})" if tx.source else ""
                if tx.kind == "referral_bonus" and tx.related_user_id:
                    suffix = f" (реферал {tx.related_user_id})"
                lines.append(f"• +{_format_money(tx.amount)} ₽ {label}{suffix}")

        expenses = entry["expenses"]
        if expenses:
            lines.append("Списания:")
            for kind in ["usage", "config_creation", "withdraw"]:
                if kind not in expenses:
                    continue
                summary = expenses[kind]
                label = KIND_LABELS.get(kind, kind)
                count = summary["count"]
                total = summary["total"]
                count_suffix = f" ×{count}" if count > 1 else ""
                lines.append(f"• -{_format_money(total)} ₽ {label}{count_suffix}")
            other_kinds = [k for k in expenses.keys() if k not in {"usage", "config_creation", "withdraw"}]
            for kind in other_kinds:
                summary = expenses[kind]
                label = KIND_LABELS.get(kind, kind)
                count = summary["count"]
                total = summary["total"]
                count_suffix = f" ×{count}" if count > 1 else ""
                lines.append(f"• -{_format_money(total)} ₽ {label}{count_suffix}")

    return "\n".join(lines)


@router.callback_query(lambda c: c.data and c.data.startswith("tx:summary:"))
async def transactions_summary(callback: CallbackQuery) -> None:
    user = await require_user(callback)
    if not user:
        return

    parts = callback.data.split(":")
    if len(parts) not in (4, 5):
        await callback.answer("Некорректные данные", show_alert=True)
        return

    try:
        page = int(parts[2])
    except ValueError:
        await callback.answer("Некорректный номер страницы", show_alert=True)
        return
    if page < 0:
        await callback.answer("Некорректный номер страницы", show_alert=True)
        return

    filter_key = parts[3]
    if filter_key not in FILTERS:
        await callback.answer("Некорректный фильтр", show_alert=True)
        return

    target_user_id = user.id
    target_user = user
    user_label = None
    if len(parts) == 5:
        if not is_admin(callback.from_user.id):
            await callback.answer("Недостаточно прав", show_alert=True)
            return
        try:
            target_user_id = int(parts[4])
        except ValueError:
            await callback.answer("Некорректный пользователь", show_alert=True)
            return
        target_user = await user_service.get(target_user_id)
        if not target_user:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        user_label = f"{target_user.username} (ID {target_user.tg_id})"

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    end_date = (now.date() - timedelta(days=page * TX_DAYS_PER_PAGE))
    start_date = end_date - timedelta(days=TX_DAYS_PER_PAGE - 1)
    start_dt = datetime.combine(start_date, time.min)
    end_dt = datetime.combine(end_date + timedelta(days=1), time.min)

    filter_spec = FILTERS[filter_key]
    kinds = filter_spec["kinds"]
    amount_sign = filter_spec["amount_sign"]

    txs = await billing_service.list_transactions(
        user_id=target_user_id,
        start=start_dt,
        end=end_dt,
        kinds=kinds,
        amount_sign=amount_sign,
    )
    expense_overview = None
    if filter_key == "expense":
        expense_overview = _summarize_expenses(txs)

    window_start = now - timedelta(days=FORECAST_WINDOW_DAYS)
    usage_txs = await billing_service.list_transactions(
        user_id=target_user_id,
        start=window_start,
        end=now,
        kinds=["usage"],
        amount_sign="negative",
    )
    usage_total = sum(
        ((-tx.amount) for tx in usage_txs if tx.amount < 0),
        Decimal("0.00"),
    )
    avg_daily_spend = usage_total / Decimal(FORECAST_WINDOW_DAYS) if usage_total > 0 else Decimal("0.00")
    forecast_label = f"по использованию, {FORECAST_WINDOW_DAYS} дн."
    if avg_daily_spend <= 0:
        expense_txs = await billing_service.list_transactions(
            user_id=target_user_id,
            start=window_start,
            end=now,
            amount_sign="negative",
        )
        expense_total = sum(
            ((-tx.amount) for tx in expense_txs if tx.amount < 0),
            Decimal("0.00"),
        )
        avg_daily_spend = (
            expense_total / Decimal(FORECAST_WINDOW_DAYS)
            if expense_total > 0
            else Decimal("0.00")
        )
        forecast_label = f"по расходам, {FORECAST_WINDOW_DAYS} дн."

    forecast_line = _forecast_text(
        balance=target_user.balance,
        avg_daily_spend=avg_daily_spend,
        now=now,
        label=forecast_label,
    )
    has_prev = page > 0
    has_next = await billing_service.has_transactions_before(
        user_id=target_user_id,
        before=start_dt,
        kinds=kinds,
        amount_sign=amount_sign,
    )

    text = _build_tx_summary(
        txs,
        start_date=start_date,
        end_date=end_date,
        user_label=user_label,
        expense_overview=expense_overview,
        forecast_line=forecast_line,
    )
    await callback.message.edit_text(
        text,
        reply_markup=transactions_filters_kb(
            page=page,
            current_filter=filter_key,
            has_prev=has_prev,
            has_next=has_next,
            user_id=target_user_id if user_label else None,
        ),
        parse_mode="HTML",
    )
    await callback.answer()
