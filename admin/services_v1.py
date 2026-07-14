from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from fastapi import Request
from sqlalchemy import func, select

from core.db.models.ledger import LedgerEntry, LedgerKind
from core.db.models.user import User
from core.db.unit_of_work import uow
from core.domain import VPNOperationKind, VPNState
from core.exceptions import InvalidOperationError, UserNotFoundError
from core.services.admin_queries import money
from core.services.config import ConfigService

from .security import AdminPrincipal, add_audit_event


class AdminIdempotencyConflict(InvalidOperationError):
    pass


class AdminOptimisticConflict(InvalidOperationError):
    pass


@dataclass(frozen=True, slots=True)
class BalanceAdjustmentCommand:
    direction: str
    amount: Decimal
    reason_code: str
    comment: str
    expected_balance: Decimal | None = None
    expected_ledger_entry_id: int | None = None

    @property
    def signed_amount(self) -> Decimal:
        return self.amount if self.direction == "credit" else -self.amount

    def request_hash(self, *, user_id: int) -> str:
        payload = {
            "user_id": user_id,
            "direction": self.direction,
            "amount": money(self.amount),
            "reason_code": self.reason_code,
            "comment": self.comment,
            "expected_balance": (
                money(self.expected_balance)
                if self.expected_balance is not None
                else None
            ),
            "expected_ledger_entry_id": self.expected_ledger_entry_id,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class AdminBalanceService:
    """Audited, idempotent administrator balance adjustments."""

    def __init__(self) -> None:
        self._config_service = ConfigService(uow)

    @staticmethod
    def _namespaced_key(*, user_id: int, client_key: str) -> str:
        digest = hashlib.sha256(client_key.encode("utf-8")).hexdigest()
        return f"admin-v1:balance:{user_id}:{digest}"

    async def adjust(
        self,
        *,
        request: Request,
        principal: AdminPrincipal,
        user_id: int,
        client_key: str,
        command: BalanceAdjustmentCommand,
    ) -> dict[str, Any]:
        if not client_key.strip() or len(client_key) > 160:
            raise InvalidOperationError("Invalid Idempotency-Key")
        if command.direction not in {"credit", "debit"}:
            raise InvalidOperationError("Invalid balance adjustment direction")
        if command.amount <= 0:
            raise InvalidOperationError("Balance adjustment amount must be positive")

        idempotency_key = self._namespaced_key(
            user_id=user_id,
            client_key=client_key,
        )
        request_hash = command.request_hash(user_id=user_id)
        operation_ids: list[str] = []
        replayed = False

        async with uow() as repos:
            session = repos["users"].session
            locked_user = await session.scalar(
                select(User).where(User.id == user_id).with_for_update()
            )
            if locked_user is None:
                raise UserNotFoundError(f"User with ID {user_id} not found")

            existing = await repos["billing"].get_ledger_entry(idempotency_key)
            if existing is not None:
                if (existing.details or {}).get("request_hash") != request_hash:
                    raise AdminIdempotencyConflict(
                        "Idempotency-Key was already used for another request"
                    )
                if (
                    existing.user_id != user_id
                    or existing.amount != command.signed_amount
                ):
                    raise AdminIdempotencyConflict(
                        "Idempotency-Key does not match the original adjustment"
                    )
                movement = existing
                previous_balance = existing.balance_after - existing.amount
                resulting_balance = existing.balance_after
                replayed = True
            else:
                latest_ledger_id = int(
                    await session.scalar(
                        select(func.max(LedgerEntry.id)).where(
                            LedgerEntry.user_id == user_id
                        )
                    )
                    or 0
                )
                if (
                    command.expected_balance is not None
                    and locked_user.balance != command.expected_balance
                ):
                    raise AdminOptimisticConflict(
                        "User balance changed since the confirmation screen"
                    )
                if (
                    command.expected_ledger_entry_id is not None
                    and latest_ledger_id != command.expected_ledger_entry_id
                ):
                    raise AdminOptimisticConflict(
                        "User ledger changed since the confirmation screen"
                    )

                previous_balance = locked_user.balance
                result = await repos["billing"].apply_balance_change(
                    user_id=user_id,
                    amount=command.signed_amount,
                    kind=LedgerKind.ADMIN_ADJUSTMENT,
                    idempotency_key=idempotency_key,
                    allow_negative_balance=False,
                    reference_type="admin_audit",
                    reference_id=getattr(request.state, "request_id", None),
                    details={
                        "source": "admin_v1",
                        "actor_user_id": principal.user_id,
                        "actor_username": principal.username,
                        "actor_role": principal.role.value,
                        "direction": command.direction,
                        "reason_code": command.reason_code,
                        "comment": command.comment,
                        "request_hash": request_hash,
                    },
                )
                movement = result.ledger_entry
                if not result.applied:
                    # ``BillingRepo`` may resolve a uniqueness race by returning
                    # the winner. Amount/kind are validated there; the request
                    # hash additionally protects reason/comment semantics.
                    if (movement.details or {}).get("request_hash") != request_hash:
                        raise AdminIdempotencyConflict(
                            "Idempotency-Key was already used for another request"
                        )
                    previous_balance = movement.balance_after - movement.amount
                    resulting_balance = movement.balance_after
                    replayed = True
                else:
                    resulting_balance = result.user.balance
                    desired_state, kind = (
                        (VPNState.ACTIVE.value, VPNOperationKind.UNSUSPEND.value)
                        if resulting_balance > 0
                        else (VPNState.SUSPENDED.value, VPNOperationKind.SUSPEND.value)
                    )
                    operation_ids = await self._config_service.prepare_entitlement(
                        repos=repos,
                        owner_id=user_id,
                        desired_state=desired_state,
                        kind=kind,
                    )

            add_audit_event(
                session,
                request,
                action=(
                    "balance.adjustment_replayed"
                    if replayed
                    else "balance.adjustment_applied"
                ),
                actor_user_id=principal.user_id,
                target_type="user",
                target_id=user_id,
                details={
                    "ledger_entry_id": movement.id,
                    "direction": command.direction,
                    "amount": money(command.amount),
                    "previous_balance": money(previous_balance),
                    "new_balance": money(resulting_balance),
                    "reason_code": command.reason_code,
                    "comment": command.comment,
                    "idempotency_key_hash": hashlib.sha256(
                        client_key.encode("utf-8")
                    ).hexdigest(),
                    "request_hash": request_hash,
                    "replayed": replayed,
                    "entitlement_operation_ids": operation_ids,
                },
            )

        completed = 0
        if operation_ids:
            completed = await self._config_service.execute_operations(
                operation_ids,
                owner_id=user_id,
            )

        return {
            "adjustment_id": getattr(request.state, "request_id", None),
            "ledger_entry_id": movement.id,
            "previous_balance": money(previous_balance),
            "new_balance": money(resulting_balance),
            "applied": not replayed,
            "replayed": replayed,
            "entitlement": {
                "state": (
                    "completed"
                    if operation_ids and completed == len(operation_ids)
                    else "queued" if operation_ids else "not_required"
                ),
                "operation_ids": operation_ids,
                "completed": completed,
            },
        }
