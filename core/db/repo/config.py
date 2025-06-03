from datetime import datetime
from typing import Sequence

from sqlalchemy import update

from core.db.models import VPN_Config

from .base import BaseRepo


class ConfigRepo(BaseRepo[VPN_Config]):
    model = VPN_Config

    async def get_active_configs(self, owner_id: int = None) -> Sequence[VPN_Config]:
        filters = {"suspended": False}
        if owner_id:
            filters["owner_id"] = owner_id
        return await self.list(**filters)

    async def suspend_config(self, config_id: int) -> VPN_Config:
        stmt = (
            update(VPN_Config)
            .where(VPN_Config.id == config_id)
            .values(suspended=True, suspended_at=datetime.now())
            .returning(VPN_Config)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.scalar_one_or_none()

    async def unsuspend_config(self, config_id: int) -> VPN_Config:
        stmt = (
            update(VPN_Config)
            .where(VPN_Config.id == config_id)
            .values(suspended=False, suspended_at=None)
            .returning(VPN_Config)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.scalar_one_or_none()
