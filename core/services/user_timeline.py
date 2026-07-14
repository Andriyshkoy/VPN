from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import String, case, cast, func, literal, select

from core.db.models.admin import AdminAuditEvent, AdminUser
from core.db.models.ledger import LedgerEntry
from core.db.models.payment import ProviderPayment
from core.db.models.referral_reward import ReferralReward
from core.db.models.telegram_user_action import TelegramUserActionEvent
from core.db.models.user import User
from core.db.models.vpn_operation import VPNOperation

from .admin_queries import money, utc_iso

TIMELINE_CATEGORIES = frozenset(
    {"bot", "finance", "referral", "vpn", "admin", "account"}
)
TIMELINE_SOURCES = frozenset(
    {"bot", "ledger", "payment", "referral", "vpn", "admin", "user"}
)


@dataclass(frozen=True, slots=True)
class _TimelineRecord:
    record_id: int
    source: str
    category: str
    action: str
    result: str
    occurred_at: datetime
    title: str
    description: str | None
    metadata: dict[str, object]
    actor: dict[str, object] | None


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _bounded_text(value: object, *, limit: int = 500) -> str:
    return str(value)[:limit]


_SENSITIVE_KEYS = (
    "password",
    "token",
    "secret",
    "api_key",
    "credential",
    "private_key",
    "payload",
    "raw_data",
    "config_content",
    "ovpn",
)


def redact_admin_metadata(value: Any, *, depth: int = 0) -> Any:
    """Recursively shape legacy audit details before returning them to admins."""

    if depth >= 6:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        shaped = {}
        for raw_key, nested in list(value.items())[:100]:
            key = _bounded_text(raw_key, limit=96)
            normalized = key.casefold()
            if any(sensitive in normalized for sensitive in _SENSITIVE_KEYS):
                shaped[key] = "[REDACTED]"
            else:
                shaped[key] = redact_admin_metadata(nested, depth=depth + 1)
        return shaped
    if isinstance(value, (list, tuple)):
        return [
            redact_admin_metadata(item, depth=depth + 1) for item in list(value)[:50]
        ]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    return _bounded_text(value, limit=1_000)


_BOT_METADATA_FIELDS: dict[str, type | tuple[type, ...]] = {
    "content_type": str,
    "guide": str,
    "direction": str,
    "provider": str,
    "amount_rub": int,
    "config_id": int,
    "server_id": int,
    "attempts": int,
    "error_type": str,
    "reason_code": str,
    "flow": str,
}
_SAFE_FLOW_VALUES = frozenset({"create_config", "rename_config", "none"})


def shape_bot_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    shaped: dict[str, object] = {}
    for key, expected_type in _BOT_METADATA_FIELDS.items():
        candidate = value.get(key)
        if isinstance(candidate, bool) or not isinstance(candidate, expected_type):
            continue
        if isinstance(candidate, str):
            candidate = candidate[:64]
            if key == "flow" and candidate not in _SAFE_FLOW_VALUES:
                continue
        elif isinstance(candidate, int):
            candidate = max(0, candidate)
        shaped[key] = candidate
    return shaped


_BOT_TITLES = {
    "navigation.start": "Открыл бота",
    "navigation.menu": "Открыл главное меню",
    "navigation.help": "Открыл помощь",
    "navigation.instructions_open": "Открыл инструкции",
    "navigation.guide_open": "Открыл инструкцию для устройства",
    "navigation.cancel": "Отменил действие",
    "finance.balance_view": "Проверил баланс",
    "finance.balance_history": "Открыл историю баланса",
    "finance.topup_open": "Запросил пополнение баланса",
    "finance.payment_provider_select": "Выбрал способ оплаты",
    "finance.payment_amount_select": "Выбрал сумму пополнения",
    "finance.payment_pre_checkout": "Проверка платежа в Telegram",
    "finance.payment_successful": "Telegram сообщил об успешном платеже",
    "referral.overview": "Открыл реферальную программу",
    "vpn.config_list": "Запросил список конфигураций",
    "vpn.config_create_start": "Запросил создание конфигурации",
    "vpn.config_create_submit": "Отправил данные для создания конфигурации",
    "vpn.config_server_select": "Выбрал VPN-сервер",
    "vpn.config_view": "Запросил просмотр конфигурации",
    "vpn.config_suspend": "Запросил приостановку конфигурации",
    "vpn.config_resume": "Запросил возобновление конфигурации",
    "vpn.config_delete_request": "Запросил удаление конфигурации",
    "vpn.config_delete_confirm": "Подтвердил удаление конфигурации",
    "vpn.config_download": "Запросил скачивание конфигурации",
    "vpn.config_rename_start": "Начал переименование конфигурации",
    "vpn.config_rename_submit": "Отправил новое название конфигурации",
    "access.invite_lookup": "Открыл бота по приглашению",
    "access.invite_required": "Попытался открыть бота без приглашения",
    "message.unrecognized": "Отправил нераспознанное сообщение",
    "message.received": "Отправил сообщение боту",
    "message.command_received": "Отправил неизвестную команду",
    "callback.received": "Нажал устаревшую или неизвестную кнопку",
    "update.received": "Отправил событие боту",
    "privacy.non_private_input": "Обратился к боту вне личного чата",
}


class AdminUserTimelineService:
    """Build one permission-shaped, globally ordered user activity page."""

    MAX_PAGE_SIZE = 100
    MAX_OFFSET = 10_000

    def __init__(self, uow: Callable):
        self._uow = uow

    async def list_timeline(
        self,
        user_id: int,
        *,
        category: str | None = None,
        action: str | None = None,
        result: str | None = None,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
        include_finance: bool = False,
        include_referral: bool = False,
        include_vpn: bool = False,
        include_admin: bool = False,
    ) -> dict[str, Any] | None:
        if category is not None and category not in TIMELINE_CATEGORIES:
            raise ValueError("invalid timeline category")
        if occurred_from is not None:
            occurred_from = _aware(occurred_from)
        if occurred_to is not None:
            occurred_to = _aware(occurred_to)
        if occurred_from is not None and occurred_to is not None:
            if occurred_from >= occurred_to:
                raise ValueError("timeline 'from' must be earlier than 'to'")
        limit = min(max(int(limit), 1), self.MAX_PAGE_SIZE)
        offset = min(max(int(offset), 0), self.MAX_OFFSET)
        window = offset + limit

        records: list[_TimelineRecord] = []
        total = 0
        async with self._uow() as repos:
            session = repos.users.session
            user = await session.get(User, user_id)
            if user is None:
                return None

            source_loaders = [
                self._account_records,
                self._bot_records,
            ]
            if include_finance:
                source_loaders.extend((self._ledger_records, self._payment_records))
            if include_referral:
                source_loaders.append(self._referral_records)
            if include_vpn:
                source_loaders.append(self._vpn_records)
            if include_admin:
                source_loaders.append(self._admin_records)

            for loader in source_loaders:
                loaded, count = await loader(
                    session,
                    user,
                    category=category,
                    action=action,
                    result=result,
                    occurred_from=occurred_from,
                    occurred_to=occurred_to,
                    window=window,
                )
                records.extend(
                    self._shape_permissions(
                        item,
                        include_finance=include_finance,
                        include_vpn=include_vpn,
                    )
                    for item in loaded
                )
                total += count

        source_order = {
            "bot": 7,
            "admin": 6,
            "vpn": 5,
            "payment": 4,
            "ledger": 3,
            "referral": 2,
            "user": 1,
        }
        records.sort(
            key=lambda item: (
                _aware(item.occurred_at),
                source_order[item.source],
                item.record_id,
            ),
            reverse=True,
        )
        page = records[offset:window]
        return {
            "items": [self._payload(item) for item in page],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @staticmethod
    def _payload(item: _TimelineRecord) -> dict[str, object]:
        return {
            "id": f"{item.source}:{item.record_id}",
            "source": item.source,
            "category": item.category,
            "action": item.action,
            "result": item.result,
            "occurred_at": utc_iso(item.occurred_at),
            "title": item.title,
            "description": item.description,
            "metadata": item.metadata,
            "actor": item.actor,
        }

    @staticmethod
    def _shape_permissions(
        item: _TimelineRecord,
        *,
        include_finance: bool,
        include_vpn: bool,
    ) -> _TimelineRecord:
        metadata = dict(item.metadata)
        if item.source == "bot":
            if not include_finance:
                for key in ("amount_rub", "provider", "direction"):
                    metadata.pop(key, None)
            if not include_vpn:
                for key in ("config_id", "server_id"):
                    metadata.pop(key, None)
        elif (
            item.source == "admin"
            and item.action.startswith("balance.")
            and not include_finance
        ):
            metadata = {
                key: metadata[key]
                for key in ("outcome", "result", "error_code", "reason_code")
                if key in metadata
            }
        return replace(item, metadata=metadata)

    @staticmethod
    def _common_conditions(
        occurred_column,
        *,
        occurred_from: datetime | None,
        occurred_to: datetime | None,
    ) -> list[Any]:
        conditions = []
        if occurred_from is not None:
            conditions.append(occurred_column >= occurred_from)
        if occurred_to is not None:
            conditions.append(occurred_column < occurred_to)
        return conditions

    async def _account_records(
        self,
        session,
        user: User,
        *,
        category,
        action,
        result,
        occurred_from,
        occurred_to,
        window,
    ):
        if category not in (None, "account"):
            return [], 0
        if action not in (None, "account.created") or result not in (None, "completed"):
            return [], 0
        occurred = user.created
        aware = _aware(occurred)
        if occurred_from is not None and aware < _aware(occurred_from):
            return [], 0
        if occurred_to is not None and aware >= _aware(occurred_to):
            return [], 0
        return [
            _TimelineRecord(
                record_id=user.id,
                source="user",
                category="account",
                action="account.created",
                result="completed",
                occurred_at=occurred,
                title="Пользователь зарегистрирован",
                description=None,
                metadata={},
                actor={"type": "user", "id": user.id, "label": user.username},
            )
        ], 1

    async def _bot_records(
        self,
        session,
        user: User,
        *,
        category,
        action,
        result,
        occurred_from,
        occurred_to,
        window,
    ):
        if category not in (None, "bot"):
            return [], 0
        conditions = [TelegramUserActionEvent.user_id == user.id]
        if action is not None:
            conditions.append(TelegramUserActionEvent.action == action)
        if result is not None:
            conditions.append(TelegramUserActionEvent.result == result)
        conditions.extend(
            self._common_conditions(
                TelegramUserActionEvent.occurred_at,
                occurred_from=occurred_from,
                occurred_to=occurred_to,
            )
        )
        total = int(
            await session.scalar(
                select(func.count())
                .select_from(TelegramUserActionEvent)
                .where(*conditions)
            )
            or 0
        )
        rows = (
            await session.scalars(
                select(TelegramUserActionEvent)
                .where(*conditions)
                .order_by(
                    TelegramUserActionEvent.occurred_at.desc(),
                    TelegramUserActionEvent.id.desc(),
                )
                .limit(window)
            )
        ).all()
        return [
            _TimelineRecord(
                record_id=row.id,
                source="bot",
                category="bot",
                action=row.action,
                result=row.result,
                occurred_at=row.occurred_at,
                title=_BOT_TITLES.get(row.action, "Действие в боте"),
                description=None,
                metadata=shape_bot_metadata(row.metadata_json),
                actor={"type": "user", "id": user.id, "label": user.username},
            )
            for row in rows
        ], total

    async def _ledger_records(
        self,
        session,
        user: User,
        *,
        category,
        action,
        result,
        occurred_from,
        occurred_to,
        window,
    ):
        if category not in (None, "finance") or result not in (None, "completed"):
            return [], 0
        action_expr = literal("ledger.") + LedgerEntry.kind
        conditions = [LedgerEntry.user_id == user.id]
        if action is not None:
            conditions.append(action_expr == action)
        conditions.extend(
            self._common_conditions(
                LedgerEntry.created_at,
                occurred_from=occurred_from,
                occurred_to=occurred_to,
            )
        )
        total = int(
            await session.scalar(
                select(func.count()).select_from(LedgerEntry).where(*conditions)
            )
            or 0
        )
        rows = (
            await session.scalars(
                select(LedgerEntry)
                .where(*conditions)
                .order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc())
                .limit(window)
            )
        ).all()
        return [
            _TimelineRecord(
                record_id=row.id,
                source="ledger",
                category="finance",
                action=f"ledger.{row.kind}",
                result="completed",
                occurred_at=row.created_at,
                title="Начисление" if row.amount > 0 else "Списание",
                description=None,
                metadata={
                    "amount": money(row.amount),
                    "balance_after": money(row.balance_after),
                    "kind": row.kind,
                    "reference_type": row.reference_type,
                },
                actor={"type": "system"},
            )
            for row in rows
        ], total

    async def _payment_records(
        self,
        session,
        user: User,
        *,
        category,
        action,
        result,
        occurred_from,
        occurred_to,
        window,
    ):
        if category not in (None, "finance"):
            return [], 0
        occurred_expr = func.coalesce(
            ProviderPayment.credited_at, ProviderPayment.created_at
        )
        action_expr = literal("payment.") + ProviderPayment.status
        conditions = [ProviderPayment.user_id == user.id]
        if action is not None:
            conditions.append(action_expr == action)
        if result is not None:
            conditions.append(ProviderPayment.status == result)
        conditions.extend(
            self._common_conditions(
                occurred_expr,
                occurred_from=occurred_from,
                occurred_to=occurred_to,
            )
        )
        total = int(
            await session.scalar(
                select(func.count()).select_from(ProviderPayment).where(*conditions)
            )
            or 0
        )
        rows = (
            await session.scalars(
                select(ProviderPayment)
                .where(*conditions)
                .order_by(occurred_expr.desc(), ProviderPayment.id.desc())
                .limit(window)
            )
        ).all()
        return [
            _TimelineRecord(
                record_id=row.id,
                source="payment",
                category="finance",
                action=f"payment.{row.status}",
                result=row.status,
                occurred_at=row.credited_at or row.created_at,
                title=(
                    "Платёж зачислен" if row.status == "credited" else "Платёж создан"
                ),
                description=None,
                metadata={
                    "amount": money(row.amount),
                    "currency": row.currency,
                    "provider": row.provider,
                    "referral_settlement_status": row.referral_settlement_status,
                },
                actor={"type": "system"},
            )
            for row in rows
        ], total

    async def _referral_records(
        self,
        session,
        user: User,
        *,
        category,
        action,
        result,
        occurred_from,
        occurred_to,
        window,
    ):
        if category not in (None, "referral") or result not in (None, "completed"):
            return [], 0
        action_expr = literal("referral.reward_l") + cast(ReferralReward.level, String)
        conditions = [ReferralReward.beneficiary_user_id == user.id]
        if action is not None:
            conditions.append(action_expr == action)
        conditions.extend(
            self._common_conditions(
                ReferralReward.created_at,
                occurred_from=occurred_from,
                occurred_to=occurred_to,
            )
        )
        total = int(
            await session.scalar(
                select(func.count()).select_from(ReferralReward).where(*conditions)
            )
            or 0
        )
        rows = (
            await session.scalars(
                select(ReferralReward)
                .where(*conditions)
                .order_by(ReferralReward.created_at.desc(), ReferralReward.id.desc())
                .limit(window)
            )
        ).all()
        return [
            _TimelineRecord(
                record_id=row.id,
                source="referral",
                category="referral",
                action=f"referral.reward_l{row.level}",
                result="completed",
                occurred_at=row.created_at,
                title=f"Реферальное начисление · уровень {row.level}",
                description=None,
                metadata={
                    "level": row.level,
                    "rate_bps": row.rate_bps,
                    "source_user_id": row.source_user_id,
                    "source_amount": money(row.source_amount),
                    "reward_amount": money(row.reward_amount),
                    "currency": row.currency,
                    "program_version": row.program_version,
                },
                actor={"type": "system"},
            )
            for row in rows
        ], total

    async def _vpn_records(
        self,
        session,
        user: User,
        *,
        category,
        action,
        result,
        occurred_from,
        occurred_to,
        window,
    ):
        if category not in (None, "vpn"):
            return [], 0
        action_expr = literal("vpn.") + VPNOperation.kind
        conditions = [VPNOperation.owner_id == user.id]
        if action is not None:
            conditions.append(action_expr == action)
        if result is not None:
            conditions.append(VPNOperation.status == result)
        conditions.extend(
            self._common_conditions(
                VPNOperation.created_at,
                occurred_from=occurred_from,
                occurred_to=occurred_to,
            )
        )
        total = int(
            await session.scalar(
                select(func.count()).select_from(VPNOperation).where(*conditions)
            )
            or 0
        )
        rows = (
            await session.scalars(
                select(VPNOperation)
                .where(*conditions)
                .order_by(VPNOperation.created_at.desc(), VPNOperation.id.desc())
                .limit(window)
            )
        ).all()
        return [
            _TimelineRecord(
                record_id=row.id,
                source="vpn",
                category="vpn",
                action=f"vpn.{row.kind}",
                result=row.status,
                occurred_at=row.created_at,
                title="Операция с VPN-конфигурацией",
                description=None,
                metadata={
                    "operation_id": row.operation_id,
                    "config_id": row.config_id,
                    "server_id": row.server_id,
                    "kind": row.kind,
                    "attempts": row.attempts,
                },
                actor={"type": "system"},
            )
            for row in rows
        ], total

    async def _admin_records(
        self,
        session,
        user: User,
        *,
        category,
        action,
        result,
        occurred_from,
        occurred_to,
        window,
    ):
        if category not in (None, "admin"):
            return [], 0
        result_expr = func.coalesce(
            AdminAuditEvent.details["outcome"].as_string(),
            AdminAuditEvent.details["result"].as_string(),
            case(
                (AdminAuditEvent.action.like("%.failed"), "failed"),
                else_="completed",
            ),
        )
        conditions = [
            AdminAuditEvent.target_type == "user",
            AdminAuditEvent.target_id == str(user.id),
        ]
        if action is not None:
            conditions.append(AdminAuditEvent.action == action)
        if result is not None:
            conditions.append(result_expr == result)
        conditions.extend(
            self._common_conditions(
                AdminAuditEvent.created_at,
                occurred_from=occurred_from,
                occurred_to=occurred_to,
            )
        )
        total = int(
            await session.scalar(
                select(func.count()).select_from(AdminAuditEvent).where(*conditions)
            )
            or 0
        )
        rows = (
            await session.execute(
                select(AdminAuditEvent, AdminUser.username.label("actor_username"))
                .outerjoin(AdminUser, AdminUser.id == AdminAuditEvent.actor_user_id)
                .where(*conditions)
                .order_by(AdminAuditEvent.created_at.desc(), AdminAuditEvent.id.desc())
                .limit(window)
            )
        ).all()
        result_records = []
        for joined in rows:
            row = joined.AdminAuditEvent
            details = redact_admin_metadata(dict(row.details or {}))
            event_result = (
                details.get("outcome")
                or details.get("result")
                or ("failed" if row.action.endswith(".failed") else "completed")
            )
            actor = (
                {
                    "type": "admin",
                    "id": row.actor_user_id,
                    "label": joined.actor_username,
                }
                if row.actor_user_id is not None
                else {"type": "system"}
            )
            result_records.append(
                _TimelineRecord(
                    record_id=row.id,
                    source="admin",
                    category="admin",
                    action=row.action,
                    result=_bounded_text(event_result, limit=32),
                    occurred_at=row.created_at,
                    title="Действие администратора",
                    description=None,
                    metadata=details,
                    actor=actor,
                )
            )
        return result_records, total
