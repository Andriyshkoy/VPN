"""Transactional product state; technical identity history remains in bindings."""

from __future__ import annotations

from sqlalchemy import select

from core.db.models.subscription import Subscription, SubscriptionAccess

from .base import BaseRepo


class SubscriptionRepo(BaseRepo[Subscription]):
    model = Subscription

    async def get_by_user(
        self, user_id: int, *, for_update: bool = False
    ) -> Subscription | None:
        stmt = select(Subscription).where(Subscription.user_id == user_id)
        if for_update:
            stmt = stmt.with_for_update()
        return await self.session.scalar(stmt)

    async def get_for_update(self, user_id: int) -> Subscription | None:
        return await self.get_by_user(user_id, for_update=True)

    async def create(
        self,
        user_id: int,
        plan_code: str,
        *,
        status: str = "pending",
        **fields,
    ) -> Subscription:
        allowed = {
            "id",
            "migration_source",
            "migration_snapshot",
            "version",
        }
        if set(fields) - allowed:
            raise ValueError("Unsupported subscription fields")
        row = Subscription(
            user_id=user_id, plan_code=plan_code, status=status, **fields
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def update(self, subscription_id: str, **fields) -> Subscription:
        allowed = {
            "plan_code",
            "status",
            "migration_source",
            "migration_snapshot",
            "version",
        }
        if set(fields) - allowed:
            raise ValueError("Unsupported subscription fields")
        row = await self.session.get(Subscription, subscription_id)
        if row is None:
            raise ValueError("Subscription not found")
        for key, value in fields.items():
            setattr(row, key, value)
        await self.session.flush()
        return row

    async def list_accesses(self, subscription_id: str) -> list[SubscriptionAccess]:
        return list(
            (
                await self.session.scalars(
                    select(SubscriptionAccess)
                    .where(SubscriptionAccess.subscription_id == subscription_id)
                    .order_by(SubscriptionAccess.service_id)
                )
            ).all()
        )

    async def get_access(
        self, subscription_id: str, service_id: str, *, for_update: bool = False
    ) -> SubscriptionAccess | None:
        stmt = select(SubscriptionAccess).where(
            SubscriptionAccess.subscription_id == subscription_id,
            SubscriptionAccess.service_id == service_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await self.session.scalar(stmt)

    async def ensure_access(
        self, subscription_id: str, service_id: str, **fields
    ) -> SubscriptionAccess:
        row = await self.get_access(subscription_id, service_id, for_update=True)
        if row is not None:
            return row
        allowed = {"config_id", "generation", "provision_operation_id"}
        if set(fields) - allowed:
            raise ValueError("Unsupported subscription access fields")
        row = SubscriptionAccess(
            subscription_id=subscription_id, service_id=service_id, **fields
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def update_access(self, access_id: int, **fields) -> SubscriptionAccess:
        allowed = {"config_id", "generation", "provision_operation_id"}
        if set(fields) - allowed:
            raise ValueError("Unsupported subscription access fields")
        row = await self.session.get(SubscriptionAccess, access_id)
        if row is None:
            raise ValueError("Subscription access not found")
        for key, value in fields.items():
            setattr(row, key, value)
        await self.session.flush()
        return row
