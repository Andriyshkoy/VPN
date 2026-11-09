"""Queries shared by reviewed campaigns and their delivery guard."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.models.campaign import AdminCampaign, CampaignOperation, CampaignRecipient
from core.db.models.notification_outbox import NotificationOutbox
from core.db.models.user import User


class CampaignRepo:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, public_id: str, *, lock: bool = False):
        stmt = select(AdminCampaign).where(AdminCampaign.public_id == public_id)
        if lock:
            stmt = stmt.with_for_update()
        return await self.session.scalar(stmt)

    async def operation(self, campaign_id: int, kind: str, key_hash: str):
        return await self.session.scalar(
            select(CampaignOperation).where(
                CampaignOperation.campaign_id == campaign_id,
                CampaignOperation.kind == kind,
                CampaignOperation.key_hash == key_hash,
            )
        )

    async def audience(self, audience: dict):
        stmt = select(User).order_by(User.id)
        if audience["kind"] == "selected":
            stmt = stmt.where(User.id.in_(audience["user_ids"]))
        users = list((await self.session.scalars(stmt)).all())
        eligible = [
            u for u in users if u.tg_id > 0 and u.telegram_delivery_status == "active"
        ]
        missing = (
            len(set(audience.get("user_ids", [])) - {u.id for u in users})
            if audience["kind"] == "selected"
            else 0
        )
        return eligible, len(users) - len(eligible), missing

    async def counts(self, campaign_id: int):
        rows = (
            await self.session.execute(
                select(
                    CampaignRecipient.status, NotificationOutbox.status, func.count()
                )
                .outerjoin(
                    NotificationOutbox,
                    CampaignRecipient.outbox_id == NotificationOutbox.id,
                )
                .where(CampaignRecipient.campaign_id == campaign_id)
                .group_by(CampaignRecipient.status, NotificationOutbox.status)
            )
        ).all()
        counts = {
            name: 0
            for name in (
                "pending",
                "queued",
                "sending",
                "delivered",
                "failed",
                "unknown",
                "skipped",
                "cancelled",
            )
        }
        for status, outbox_status, count in rows:
            visible = (
                "queued"
                if status == "pending" and outbox_status == "queued"
                else status
            )
            counts[visible] += count
        return {"total": sum(counts.values()), **counts}

    async def recipients(
        self, campaign_id: int, *, status: str | None = None, lock=False
    ):
        stmt = (
            select(CampaignRecipient)
            .where(CampaignRecipient.campaign_id == campaign_id)
            .order_by(CampaignRecipient.id)
        )
        if status:
            stmt = stmt.where(CampaignRecipient.status == status)
        if lock:
            stmt = stmt.with_for_update()
        return list((await self.session.scalars(stmt)).all())
