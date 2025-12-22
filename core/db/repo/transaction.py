from datetime import datetime
from decimal import Decimal
from typing import Sequence

from sqlalchemy import func, select

from core.db.models import BalanceTransaction

from .base import BaseRepo


class TransactionRepo(BaseRepo[BalanceTransaction]):
    model = BalanceTransaction

    async def create(
        self,
        *,
        user_id: int,
        amount: Decimal,
        kind: str,
        source: str,
        description: str | None = None,
        config_id: int | None = None,
        related_user_id: int | None = None,
    ) -> BalanceTransaction:
        tx = self.model(
            user_id=user_id,
            amount=amount,
            kind=kind,
            source=source,
            description=description,
            config_id=config_id,
            related_user_id=related_user_id,
        )
        return await self.add(tx)

    async def list_for_user(
        self,
        *,
        user_id: int,
        limit: int | None = None,
        offset: int = 0,
        kinds: Sequence[str] | None = None,
        amount_sign: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Sequence[BalanceTransaction]:
        stmt = select(self.model).where(self.model.user_id == user_id)
        if kinds:
            stmt = stmt.where(self.model.kind.in_(kinds))
        if amount_sign == "positive":
            stmt = stmt.where(self.model.amount > 0)
        elif amount_sign == "negative":
            stmt = stmt.where(self.model.amount < 0)
        if start is not None:
            stmt = stmt.where(self.model.created_at >= start)
        if end is not None:
            stmt = stmt.where(self.model.created_at < end)
        stmt = (
            stmt.order_by(self.model.created_at.desc(), self.model.id.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self.session.scalars(stmt)
        return result.all()

    async def exists_before(
        self,
        *,
        user_id: int,
        before: datetime,
        kinds: Sequence[str] | None = None,
        amount_sign: str | None = None,
    ) -> bool:
        stmt = select(self.model.id).where(
            self.model.user_id == user_id,
            self.model.created_at < before,
        )
        if kinds:
            stmt = stmt.where(self.model.kind.in_(kinds))
        if amount_sign == "positive":
            stmt = stmt.where(self.model.amount > 0)
        elif amount_sign == "negative":
            stmt = stmt.where(self.model.amount < 0)
        stmt = stmt.limit(1)
        result = await self.session.scalar(stmt)
        return result is not None

    async def sum_referral_bonus_by_related(
        self,
        *,
        user_id: int,
        related_user_ids: Sequence[int],
    ) -> dict[int, Decimal]:
        if not related_user_ids:
            return {}
        stmt = (
            select(self.model.related_user_id, func.sum(self.model.amount))
            .where(
                self.model.user_id == user_id,
                self.model.kind == "referral_bonus",
                self.model.related_user_id.in_(related_user_ids),
            )
            .group_by(self.model.related_user_id)
        )
        rows = await self.session.execute(stmt)
        return {
            int(related_id): total or Decimal("0.00")
            for related_id, total in rows.all()
            if related_id is not None
        }
