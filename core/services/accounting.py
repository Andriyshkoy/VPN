from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable

from sqlalchemy import func, select

from core.db.models.ledger import LedgerEntry
from core.db.models.user import User
from core.exceptions import InvalidOperationError, UserNotFoundError


@dataclass(frozen=True, slots=True)
class BalanceHistoryItem:
    """One immutable movement in a user's balance."""

    id: int
    amount: Decimal
    balance_after: Decimal
    kind: str
    reference_type: str | None
    reference_id: str | None
    details: dict
    created_at: datetime


@dataclass(frozen=True, slots=True)
class BalanceHistoryPage:
    """A bounded, newest-first page of balance movements."""

    items: tuple[BalanceHistoryItem, ...]
    total: int
    limit: int
    offset: int
    snapshot_id: int


class AccountingService:
    """Read-only access to the immutable balance ledger."""

    MAX_PAGE_SIZE = 50

    def __init__(self, uow: Callable):
        self._uow = uow

    async def list_balance_history(
        self,
        user_id: int,
        *,
        limit: int = 8,
        offset: int = 0,
        snapshot_id: int | None = None,
    ) -> BalanceHistoryPage:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= self.MAX_PAGE_SIZE
        ):
            raise InvalidOperationError("Invalid balance history page size")
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or not 0 <= offset <= 1_000_000
        ):
            raise InvalidOperationError("Invalid balance history offset")
        if snapshot_id is not None and (
            isinstance(snapshot_id, bool)
            or not isinstance(snapshot_id, int)
            or not 0 <= snapshot_id <= 9_223_372_036_854_775_807
        ):
            raise InvalidOperationError("Invalid balance history snapshot")

        async with self._uow() as repos:
            session = repos["users"].session
            if await session.get(User, user_id) is None:
                raise UserNotFoundError(f"User with ID {user_id} not found")

            if snapshot_id is None:
                snapshot_id = int(
                    await session.scalar(
                        select(func.max(LedgerEntry.id)).where(
                            LedgerEntry.user_id == user_id
                        )
                    )
                    or 0
                )
            history_scope = (
                LedgerEntry.user_id == user_id,
                LedgerEntry.id <= snapshot_id,
            )

            total = int(
                await session.scalar(
                    select(func.count()).select_from(LedgerEntry).where(*history_scope)
                )
                or 0
            )
            rows = (
                await session.scalars(
                    select(LedgerEntry)
                    .where(*history_scope)
                    .order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ).all()

        return BalanceHistoryPage(
            items=tuple(
                BalanceHistoryItem(
                    id=row.id,
                    amount=row.amount,
                    balance_after=row.balance_after,
                    kind=row.kind,
                    reference_type=row.reference_type,
                    reference_id=row.reference_id,
                    details=dict(row.details or {}),
                    created_at=row.created_at,
                )
                for row in rows
            ),
            total=total,
            limit=limit,
            offset=offset,
            snapshot_id=snapshot_id,
        )
