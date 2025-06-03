from __future__ import annotations
from datetime import datetime
from typing import Any, Mapping, Sequence

from core.db.models import VPN_Config

from .api_gateway import APIGateway


class ConfigService:
    """High‑level operations on *VPN client configurations*."""

    def __init__(self, repos: Mapping[str, Any]) -> None:
        self._configs = repos["configs"]
        self._servers = repos["servers"]

    # ---------- CRUD wrappers ----------

    async def create_config(
        self,
        *,
        server_id: int,
        owner_id: int,
        name: str,
        use_password: bool = False,
    ) -> VPN_Config:
        server = await self._servers.get(id=server_id)
        if not server:
            raise ValueError(f"Server {server_id} not found")

        # provision on remote server first
        async with APIGateway(server) as api:
            await api.create_client(name, use_password=use_password)

        cfg = VPN_Config(
            name=name,
            server_id=server_id,
            owner_id=owner_id,
            suspended=False,
            created_at=datetime.now(),
        )
        return await self._configs.add(cfg)

    async def download_config(self, config_id: int) -> bytes:
        cfg = await self._configs.get(id=config_id, joined_load=["server"])
        if not cfg:
            raise ValueError("Config not found")
        async with APIGateway(cfg.server) as api:
            return await api.download_config(cfg.name)

    async def revoke_config(self, config_id: int) -> None:
        cfg = await self._configs.get(id=config_id, joined_load=["server"])
        if not cfg:
            raise ValueError("Config not found")
        async with APIGateway(cfg.server) as api:
            await api.revoke_client(cfg.name)
        await self._configs.delete(id=config_id)

    async def suspend_config(self, config_id: int) -> VPN_Config:
        cfg = await self._configs.get(id=config_id, joined_load=["server"])
        if not cfg:
            raise ValueError("Config not found")
        async with APIGateway(cfg.server) as api:
            await api.suspend_client(cfg.name)
        return await self._configs.suspend_config(config_id)

    async def unsuspend_config(self, config_id: int) -> VPN_Config:
        cfg = await self._configs.get(id=config_id, joined_load=["server"])
        if not cfg:
            raise ValueError("Config not found")
        async with APIGateway(cfg.server) as api:
            await api.unsuspend_client(cfg.name)
        return await self._configs.unsuspend_config(config_id)

    async def list_active(self, *, owner_id: int | None = None) -> Sequence[VPN_Config]:
        return await self._configs.get_active_configs(owner_id=owner_id)

    async def list_suspended(
        self, *, owner_id: int | None = None
    ) -> Sequence[VPN_Config]:
        return await self._configs.get_suspended_configs(owner_id=owner_id)

    async def list_blocked(self, server_id: int) -> Sequence[str]:
        server = await self._servers.get(id=server_id)
        if not server:
            raise ValueError(f"Server {server_id} not found")
        async with APIGateway(server) as api:
            return await api.list_blocked()
