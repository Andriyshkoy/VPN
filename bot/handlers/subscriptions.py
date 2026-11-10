"""Account subscriptions and free, owner-scoped Hiddify credentials."""

from __future__ import annotations

import html
import re
from uuid import NAMESPACE_URL, uuid5

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from core.config import settings
from core.exceptions import APIConnectionError, InvalidOperationError, ServiceError
from core.services import TelegramActionAuditContext
from core.services.subscriptions import SubscriptionUnavailableError

from ..hiddify_profiles import hysteria_profile, vless_profile
from ..subscription_copy import (
    PLAN_DESCRIPTIONS,
    PLAN_LABELS,
    monthly_price,
    plan_overview,
)
from ..ui import format_money, safe_callback_answer, safe_edit_text
from .base import get_or_create_user, router, subscription_service

SERVICES = {"hysteria": "Hysteria 2", "vless-reality": "VLESS + REALITY"}
PLANS = PLAN_LABELS
STATUS = {
    "pending": "⏳ Ожидает настройки",
    "active": "🟢 Активна",
    "suspended": "⏸ Приостановлена",
    "expired": "⏸ Недостаточно средств",
    "cancelled": "Отключена",
}
ACCESS_STATUS = {
    "active": "🟢 готов",
    "pending": "⏳ создаётся",
    "provisioning": "⏳ создаётся",
    "suspended": "⏸ приостановлен",
    "failed": "⚠️ нужна проверка",
    "revoked": "отключён",
    "cancelled": "отключён",
}


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _markup(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _audit(context, action, *, result="completed", **metadata):
    if context is not None:
        context.record(action, result=result, metadata=metadata)


def _request_key(user_id: int, action: str, token: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"vpn-subscription:{user_id}:{action}:{token}"))


def _token(callback: CallbackQuery) -> str:
    return uuid5(
        NAMESPACE_URL, f"vpn-confirm:{callback.from_user.id}:{callback.id}"
    ).hex[:20]


def render_subscription(view, balance=None) -> str:
    if view is None:
        return (
            "💎 <b>Подписка</b>\n\nВыберите тариф:\n"
            + plan_overview()
            + "\n\nОдна подписка на аккаунт, число ключей цену не увеличивает. "
            "Выпуск и замена ключей бесплатны. "
            "Чтобы остановить оплату, приостановите подписку в боте: отключение Hiddify её не приостанавливает."
            + (
                "\n\n⏸ Списания сейчас приостановлены."
                if settings.maintenance_mode or not settings.billing_enabled
                else ""
            )
        )
    text = (
        "💎 <b>Подписка</b>\n\n"
        f"Тариф: <b>{PLANS.get(view.plan_code, 'Не выбран')}</b>\n"
        f"Стоимость: <b>{monthly_price(view)}</b>.\n"
        f"Состояние: {STATUS.get(view.status, '⏳ Обновляется')}\n"
        "Сервисы: "
        + ", ".join(SERVICES[code] for code in view.services if code in SERVICES)
    )
    text += "\nПодключения: " + (
        "одно одновременно" if view.max_sessions == 1 else "без ограничения"
    )
    if view.unlimited_balance:
        text += "\nБаланс: <b>∞</b>. Списаний нет."
    elif balance is not None:
        text += f"\nБаланс: <b>{format_money(balance)} ₽</b>."
    if view.billing_paused:
        text += "\n\n⏸ Списания сейчас приостановлены. Баланс не расходуется."
    text += (
        f"\n\n{PLAN_DESCRIPTIONS.get(view.plan_code, '')}\n\n"
        "Одна подписка на аккаунт. Выпуск и замена ключей бесплатны. "
        "Чтобы остановить оплату, приостановите подписку в боте: отключение Hiddify её не приостанавливает."
    )
    return text


def subscription_keyboard(view) -> InlineKeyboardMarkup:
    rows = [[_button("💎 Выбрать / сменить тариф", "sub:plans")]]
    if view is not None:
        rows.append([_button("🔑 Мои ключи", "sub:keys")])
        if view.status in {"active", "pending"}:
            rows.append([_button("⏸ Приостановить доступ", "sub:suspend-confirm")])
        elif view.status in {"suspended", "expired"}:
            rows.append([_button("▶️ Возобновить доступ", "sub:resume-confirm")])
    rows.append([_button("📚 Настройка Hiddify", "guide:menu")])
    return _markup(rows)


def _keys_ready(view) -> bool:
    if view is None or view.status != "active" or not view.services:
        return False
    accesses = {access.service_id: access for access in view.accesses}
    return all(
        service_id in SERVICES
        and service_id in accesses
        and accesses[service_id].status == "active"
        for service_id in view.services
    )


def _keys_feedback(view) -> str:
    if view is None:
        return "Сначала выберите тариф в разделе «Подписка»."
    if _keys_ready(view):
        return "Ключи готовы. Выберите нужный протокол."
    if view.status not in {"active", "pending"}:
        return "Доступ приостановлен. Откройте раздел «Подписка»."
    if any(
        access.status == "failed" and access.service_id in view.services
        for access in view.accesses
    ):
        return "Не все ключи готовы. Повторите выпуск или напишите в поддержку."
    available = {access.service_id for access in view.accesses}
    if any(service_id not in available for service_id in view.services):
        return "Не все ключи выпущены. Нажмите «Выпустить / проверить»."
    if any(
        access.service_id in view.services
        and access.status not in {"active", "pending", "provisioning"}
        for access in view.accesses
    ):
        return "Не все ключи готовы. Нажмите «Выпустить / проверить»."
    return "Ключи ещё готовятся. Обновите состояние чуть позже."


def render_keys(view) -> tuple[str, InlineKeyboardMarkup]:
    if view is None:
        return "🔑 <b>Мои ключи</b>\n\nСначала выберите тариф.", subscription_keyboard(
            None
        )
    text = "🔑 <b>Мои ключи</b>\n\n"
    accesses = {access.service_id: access for access in view.accesses}
    rows = []
    profile_buttons = []
    for service_id in view.services:
        if service_id not in SERVICES:
            continue
        access = accesses.get(service_id)
        state = (
            ACCESS_STATUS.get(access.status, "⏳ обновляется")
            if access
            else "ещё не выпущен"
        )
        text += f"{SERVICES[service_id]}: {state}\n"
        if access is not None and access.status == "active" and view.status == "active":
            profile_buttons.append(f"«Ключ {SERVICES[service_id]}»")
            rows.append(
                [
                    _button(
                        f"📋 Ключ {SERVICES[service_id]}", f"sub:profile:{service_id}"
                    )
                ]
            )
            rows.append(
                [
                    _button(
                        f"🔄 Заменить ключ {SERVICES[service_id]}",
                        f"sub:rotate-confirm:{service_id}",
                    )
                ]
            )
    if _keys_ready(view):
        rows.append([_button("🔄 Обновить состояние", "sub:keys")])
    elif view.status in {"active", "pending"}:
        rows.append([_button("➕ Выпустить / проверить ключи бесплатно", "sub:issue")])
    else:
        text += "\nДоступ приостановлен. Возобновить его можно в разделе «Подписка»."
    if profile_buttons:
        text += (
            "\n\nЧтобы получить профиль для Hiddify, нажмите "
            + " или ".join(profile_buttons)
            + "."
        )
    text += "\n\nСкопируйте весь профиль и в Hiddify нажмите «+» → «Из буфера обмена»."
    rows.extend(
        [[_button("💎 Подписка", "sub:view")], [_button("📚 Инструкции", "guide:menu")]]
    )
    return text, _markup(rows)


async def cmd_subscription(
    message: Message, telegram_action_audit=None, *, tg_id=None, username=None
) -> None:
    user = await get_or_create_user(
        message.from_user.id if tg_id is None else tg_id,
        message.from_user.username if tg_id is None else username,
    )
    if user is None:
        return
    view = await subscription_service.get_current(user.id)
    await message.answer(
        render_subscription(view, user.balance),
        reply_markup=subscription_keyboard(view),
    )
    _audit(telegram_action_audit, "vpn.subscription_view")


async def cmd_keys(message: Message, telegram_action_audit=None) -> None:
    user = await get_or_create_user(message.from_user.id, message.from_user.username)
    if user is None:
        return
    text, markup = render_keys(await subscription_service.get_current(user.id))
    await message.answer(text, reply_markup=markup)
    _audit(telegram_action_audit, "vpn.keys_view")


async def _invalid(callback, audit) -> None:
    await safe_callback_answer(
        callback, "Кнопка устарела. Откройте «Подписка» заново.", show_alert=True
    )
    _audit(
        audit,
        "vpn.subscription_action",
        result="invalid",
        reason_code="invalid_callback",
    )


@router.callback_query(F.data.startswith("sub:"))
async def subscription_callback(
    callback: CallbackQuery,
    state: FSMContext,
    telegram_action_audit: TelegramActionAuditContext | None = None,
) -> None:
    """Identity comes only from Telegram, never from a callback's target data."""
    await state.clear()
    parts = (callback.data or "").split(":")
    simple = len(parts) == 2 and parts[1] in {
        "view",
        "keys",
        "plans",
        "issue",
        "suspend-confirm",
        "resume-confirm",
    }
    plan_action = len(parts) == 3 and parts[1] == "choose" and parts[2] in PLANS
    plan_apply = (
        len(parts) == 4
        and parts[1] == "apply"
        and parts[2] in PLANS
        and re.fullmatch(r"0|[1-9][0-9]{0,9}", parts[3])
    )
    profile_action = (
        len(parts) == 3
        and parts[1] in {"profile", "rotate-confirm"}
        and parts[2] in SERVICES
    )
    mutation = (
        len(parts) == 4
        and parts[1] == "rotate"
        and parts[2] in SERVICES
        and re.fullmatch(r"[0-9a-f]{20}", parts[3])
    )
    state_mutation = (
        len(parts) == 3
        and parts[1] in {"suspend", "resume"}
        and re.fullmatch(r"[0-9a-f]{20}", parts[2])
    )
    if not any(
        (simple, plan_action, plan_apply, profile_action, mutation, state_mutation)
    ):
        await _invalid(callback, telegram_action_audit)
        return
    user = await get_or_create_user(callback.from_user.id, callback.from_user.username)
    if user is None:
        await safe_callback_answer(callback, "Аккаунт недоступен.", show_alert=True)
        return
    action = parts[1]
    view = await subscription_service.get_current(user.id)
    audit_action = "vpn.subscription_view"
    metadata = {}
    try:
        if action == "plans":
            rows = [
                [
                    _button(
                        f"{PLANS[plan.code]} · {monthly_price(plan)}",
                        f"sub:choose:{plan.code}",
                    )
                ]
                for plan in subscription_service.catalog()
                if plan.code in PLANS
            ]
            rows.append([_button("⬅️ Подписка", "sub:view")])
            text, markup = render_subscription(None), _markup(rows)
        elif action == "choose":
            plan = next(
                plan for plan in subscription_service.catalog() if plan.code == parts[2]
            )
            text = (
                f"💎 <b>{PLANS[plan.code]} — {monthly_price(plan)}</b>\n\n"
                f"{PLAN_DESCRIPTIONS[plan.code]}\n"
                "Выпуск и замена ключей бесплатны.\n\n"
                "Тариф изменится сразу. При переходе на BASIC прежний ключ VLESS + REALITY отключится. "
                "После смены тарифа откройте «Мои ключи» и нажмите «Выпустить / проверить». "
                "Одна подписка на аккаунт, число ключей цену не увеличивает."
            )
            if settings.maintenance_mode or not settings.billing_enabled:
                text += "\nСписания приостановлены. Выбор тарифа не спишет деньги."
            markup = _markup(
                [
                    [
                        _button(
                            "✅ Подтвердить тариф",
                            f"sub:apply:{plan.code}:{view.version if view else 0}",
                        )
                    ],
                    [_button("⬅️ Назад", "sub:plans")],
                ]
            )
        elif action in {"suspend-confirm", "resume-confirm", "rotate-confirm"}:
            operation = action.removesuffix("-confirm")
            if operation == "rotate":
                service_id = parts[2]
                if view is None or service_id not in view.services:
                    await _invalid(callback, telegram_action_audit)
                    return
                text = f"Заменить ключ <b>{SERVICES[service_id]}</b>?\n\nПрежний ключ перестанет работать на всех устройствах. Другой сервис не изменится. Замена бесплатна."
                data = f"sub:rotate:{service_id}:{_token(callback)}"
            else:
                text = (
                    "Приостановить все подключения подписки? Доступ остановится, списания прекратятся."
                    if operation == "suspend"
                    else "Возобновить доступ по текущему тарифу? Когда списания включены, для доступа нужен баланс на оплату подписки."
                )
                data = f"sub:{operation}:{_token(callback)}"
            markup = _markup(
                [[_button("✅ Подтвердить", data)], [_button("⬅️ Отмена", "sub:view")]]
            )
        elif action == "profile":
            profile = await subscription_service.export(user.id, parts[2])
            # No inline keyboard on the secret message: callback updates embed
            # their original message and are persisted in the durable inbox.
            if profile.format == "singbox-json" and parts[2] == "hysteria":
                if not isinstance(profile.content, str) or not profile.content:
                    raise InvalidOperationError("Profile content is unavailable")
                content = hysteria_profile(profile.content)
                await callback.message.answer_document(
                    BufferedInputFile(
                        content.encode("utf-8"),
                        filename="vpn-by-andriyshkoy-hysteria.json",
                    ),
                    caption="Hysteria 2 — профиль для Hiddify: импортируйте файл целиком.",
                )
                # Telegram's whole-message Copy must produce the same profile
                # as Copy Code or the file: a heading breaks JSON detection.
                secret_text = f"<pre>{html.escape(content, quote=False)}</pre>"
                if len(secret_text.encode("utf-16-le")) // 2 <= 4000:
                    await callback.message.answer(
                        secret_text, disable_web_page_preview=True
                    )
                instruction = (
                    "Hysteria выдаётся JSON-профилем с проверкой сертификата сервера. "
                    "Сообщение с кодом можно скопировать целиком: первую строку // profile-title "
                    "и JSON целиком. "
                    "В Hiddify: «+» → «Из буфера обмена» или «+» → «Файл» для .json. "
                    "Не преобразуйте его в обычную hysteria2:// ссылку и не пересылайте посторонним."
                )
            elif (
                profile.format == "uri"
                and parts[2] == "vless-reality"
                and isinstance(profile.uri, str)
            ):
                uri = vless_profile(profile.uri)
                if len(uri) <= 3500:
                    await callback.message.answer(
                        f"🔑 <b>{SERVICES[parts[2]]}</b>\n\n<pre>{html.escape(uri)}</pre>",
                        disable_web_page_preview=True,
                    )
                else:
                    await callback.message.answer_document(
                        BufferedInputFile(
                            uri.encode("utf-8"),
                            filename="vpn-by-andriyshkoy-vless-reality.txt",
                        ),
                        caption="Ключ длинный: откройте файл и скопируйте содержимое целиком.",
                    )
                instruction = (
                    "Скопируйте ключ целиком. В Hiddify: «+» → «Из буфера обмена». "
                    "Не пересылайте ключ посторонним."
                )
            else:
                raise InvalidOperationError("Profile format is unavailable")
            await callback.message.answer(
                instruction,
                reply_markup=_markup(
                    [
                        [_button("📚 Инструкции", "guide:menu")],
                        [_button("🔑 Мои ключи", "sub:keys")],
                    ]
                ),
            )
            await safe_callback_answer(callback)
            _audit(telegram_action_audit, "vpn.key_export", service_id=parts[2])
            return
        else:
            if action == "apply":
                expected_version = int(parts[3])
                if view is None or view.plan_code != parts[2]:
                    if (view is None and expected_version != 0) or (
                        view is not None and view.version != expected_version
                    ):
                        await _invalid(callback, telegram_action_audit)
                        return
                    view = await subscription_service.select_plan(
                        user.id,
                        parts[2],
                        request_key=_request_key(
                            user.id, f"plan:{parts[2]}", f"version:{expected_version}"
                        ),
                        expected_version=expected_version or None,
                    )
                audit_action, metadata = "vpn.plan_select", {"plan_code": parts[2]}
            elif action == "issue":
                if settings.maintenance_mode or not settings.provisioning_enabled:
                    await safe_callback_answer(
                        callback,
                        "Выпуск ключей временно недоступен. Попробуйте позже.",
                        show_alert=True,
                    )
                    _audit(
                        telegram_action_audit,
                        "vpn.key_issue",
                        result="unavailable",
                        reason_code="provisioning_disabled",
                    )
                    return
                view = await subscription_service.issue(
                    user.id, request_key=_request_key(user.id, "issue", callback.id)
                )
                audit_action = "vpn.key_issue"
            elif action == "rotate":
                if settings.maintenance_mode or not settings.provisioning_enabled:
                    await safe_callback_answer(
                        callback,
                        "Замена ключей временно недоступна. Попробуйте позже.",
                        show_alert=True,
                    )
                    return
                view = await subscription_service.regenerate(
                    user.id,
                    parts[2],
                    request_key=_request_key(user.id, f"rotate:{parts[2]}", parts[3]),
                )
                audit_action, metadata = "vpn.key_rotate", {"service_id": parts[2]}
            elif action in {"suspend", "resume"}:
                view = await getattr(subscription_service, action)(
                    user.id, request_key=_request_key(user.id, action, parts[2])
                )
                audit_action = f"vpn.subscription_{action}"
            if action in {"keys", "issue", "rotate"}:
                text, markup = render_keys(view)
                if action == "keys":
                    audit_action = "vpn.keys_view"
            else:
                text, markup = render_subscription(
                    view, user.balance
                ), subscription_keyboard(view)
    except APIConnectionError:
        # Preserve durable inbox retry/idempotency semantics on unknown results.
        raise
    except SubscriptionUnavailableError as exc:
        text = {
            "no_active_node": "Сервер временно недоступен для выпуска ключей. Попробуйте позже.",
            "session_limit_unavailable": "Сервер ещё не готов к тарифу с одним подключением. Деньги не списаны. Попробуйте позже или напишите в поддержку.",
        }.get(exc.reason_code, "Сервис временно недоступен. Попробуйте позже.")
        await safe_callback_answer(callback, text, show_alert=True)
        _audit(
            telegram_action_audit,
            "vpn.subscription_action",
            result="unavailable",
            reason_code=exc.reason_code,
        )
        return
    except ServiceError:
        await safe_callback_answer(
            callback,
            "Действие сейчас недоступно. Проверьте тариф и состояние доступа. Если проблема повторится, напишите в поддержку.",
            show_alert=True,
        )
        _audit(
            telegram_action_audit,
            "vpn.subscription_action",
            result="rejected",
            reason_code="request_rejected",
        )
        return
    await safe_edit_text(
        callback.message, text, reply_markup=markup, disable_web_page_preview=True
    )
    if action in {"keys", "issue"}:
        await safe_callback_answer(callback, _keys_feedback(view))
    else:
        await safe_callback_answer(callback)
    _audit(telegram_action_audit, audit_action, **metadata)
