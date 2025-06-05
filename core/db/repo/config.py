# core/db/repo/config.py
from datetime import datetime
from typing import Sequence

from sqlalchemy import update

from core.db.models import VPN_Config

from .base import BaseRepo


class ConfigRepo(BaseRepo[VPN_Config]):
    model = VPN_Config

    async def get_active(self, owner_id: int = None) -> Sequence[VPN_Config]:
        """
        Get all active (not suspended) VPN configurations.

        Args:
            owner_id: Optional ID of the owner to filter by

        Returns:
            Sequence of active VPN configurations
        """
        filters = {"suspended": False}
        if owner_id:
            filters["owner_id"] = owner_id
        return await self.list(**filters)

    async def get_suspended(self, owner_id: int = None) -> Sequence[VPN_Config]:
        """
        Get all suspended VPN configurations.

        Args:
            owner_id: Optional ID of the owner to filter by

        Returns:
            Sequence of suspended VPN configurations
        """
        filters = {"suspended": True}
        if owner_id:
            filters["owner_id"] = owner_id
        return await self.list(**filters)

    async def suspend(self, config_id: int) -> VPN_Config:
        """
        Suspend a VPN configuration by its ID.

        Args:
            config_id: ID of the configuration to suspend

        Returns:
            Updated VPN configuration or None if not found
        """
        stmt = (
            update(self.model)
            .where(self.model.id == config_id)
            .values(suspended=True, suspended_at=datetime.now())
            .returning(self.model)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.scalar_one_or_none()

    async def unsuspend(self, config_id: int) -> VPN_Config:
        """
        Remove suspension from a VPN configuration.

        Args:
            config_id: ID of the configuration to unsuspend

        Returns:
            Updated VPN configuration or None if not found
        """
        stmt = (
            update(self.model)
            .where(self.model.id == config_id)
            .values(suspended=False, suspended_at=None)
            .returning(self.model)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.scalar_one_or_none()

    async def suspend_all(self, owner_id: int) -> int:
        """Suspend all active configs for a given user."""
        stmt = (
            update(self.model)
            .where(self.model.owner_id == owner_id, self.model.suspended.is_(False))
            .values(suspended=True, suspended_at=datetime.now())
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount

    async def unsuspend_all(self, owner_id: int) -> int:
        """Unsuspend all configs for a given user."""
        stmt = (
            update(self.model)
            .where(self.model.owner_id == owner_id, self.model.suspended.is_(True))
            .values(suspended=False, suspended_at=None)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount

    async def create(
        self, server_id: int, owner_id: int, name: str, display_name: str
    ) -> VPN_Config:
        """
        Create a new VPN configuration.

        Args:
            server_id: ID of the server to associate with
            owner_id: ID of the owner
            name: Name of the configuration
            use_password: Whether to use password authentication

        Returns:
            Created VPN configuration
        """
        cfg = self.model(
            name=name,
            server_id=server_id,
            owner_id=owner_id,
            display_name=display_name,
        )
        return await self.add(cfg)
