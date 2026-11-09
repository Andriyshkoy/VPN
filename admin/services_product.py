"""Audited nonfinancial user mutations; wallet and ledger remain untouched."""

from __future__ import annotations

import hashlib
import json

from fastapi import Request
from sqlalchemy import select

import core.db as db
from core.db.models import AdminAuditEvent, User
from core.exceptions import InvalidOperationError, UserNotFoundError
from core.services.admin_queries import money

from .security import AdminPrincipal, add_audit_event
from .services_v1 import AdminIdempotencyConflict, AdminOptimisticConflict


class AdminUserFlagService:
    async def set_flag(
        self,
        *,
        request: Request,
        principal: AdminPrincipal,
        user_id: int,
        client_key: str,
        flag: str,
        enabled: bool,
        expected_enabled: bool,
        reason: str,
    ) -> dict:
        if flag not in {"unlimited_balance", "exclude_from_business_metrics"}:
            raise InvalidOperationError("Unsupported account flag")
        if not 8 <= len(client_key) <= 160 or not client_key.strip():
            raise InvalidOperationError("Invalid Idempotency-Key")
        reason = " ".join(reason.split())
        if not 3 <= len(reason) <= 500:
            raise InvalidOperationError("A meaningful reason is required")
        key_hash = hashlib.sha256(client_key.encode()).hexdigest()
        command = {
            "user_id": user_id,
            "flag": flag,
            "enabled": enabled,
            "expected_enabled": expected_enabled,
            "reason": reason,
        }
        request_hash = hashlib.sha256(
            json.dumps(command, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        action = f"user.{flag}_changed"
        async with db.async_session() as session, session.begin():
            # Serializes all flag changes and their durable idempotency results
            # across workers. Audit entries are retained with the account.
            user = await session.scalar(
                select(User).where(User.id == user_id).with_for_update()
            )
            if user is None:
                raise UserNotFoundError(user_id)
            previous = await session.scalar(
                select(AdminAuditEvent).where(
                    AdminAuditEvent.action == action,
                    AdminAuditEvent.target_type == "user",
                    AdminAuditEvent.target_id == str(user_id),
                    AdminAuditEvent.details["idempotency_key_hash"].as_string()
                    == key_hash,
                )
            )
            if previous is not None:
                if previous.details.get("request_hash") != request_hash:
                    raise AdminIdempotencyConflict(
                        "Idempotency-Key was used for a different account change"
                    )
                return {**previous.details["result"], "replayed": True}
            before = bool(getattr(user, flag))
            if before != expected_enabled:
                raise AdminOptimisticConflict(
                    "Account flag changed; refresh the account and try again"
                )
            setattr(user, flag, enabled)
            result = {
                "user_id": user_id,
                flag: enabled,
                "balance": money(user.balance),
                "balance_display": (
                    "∞" if user.unlimited_balance else money(user.balance)
                ),
                "replayed": False,
            }
            add_audit_event(
                session,
                request,
                action=action,
                actor_user_id=principal.user_id,
                target_type="user",
                target_id=user_id,
                details={
                    "before": before,
                    "after": enabled,
                    "reason": reason,
                    "idempotency_key_hash": key_hash,
                    "request_hash": request_hash,
                    "result": result,
                    "wallet_changed": False,
                    "ledger_changed": False,
                },
            )
            return result
