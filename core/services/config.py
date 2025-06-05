from __future__ import annotations
from datetime import datetime
from typing import Callable, Sequence

from .models import Config
from .api_gateway import APIGateway


class ConfigService:
    """High‑level operations on *VPN client configurations*."""

    def __init__(self, uow: Callable) -> None:
        self._uow = uow

    # ---------- CRUD wrappers ----------

    async def create_config(
        self,
        *,
        server_id: int,
        owner_id: int,
        name: str,
        display_name: str,
        use_password: bool = False,
    ) -> Config:
        async with self._uow() as repos:
            server = await repos["servers"].get(id=server_id)
            if not server:
                raise ValueError(f"Server {server_id} not found")

            async with APIGateway(server.ip, server.port, server.api_key) as api:
                await api.create_client(name, use_password=use_password)

            cfg = await repos["configs"].create(
                server_id,
                owner_id,
                name,
                display_name,
            )
            return Config.from_orm(cfg)

    async def download_config(self, config_id: int) -> bytes:
        async with self._uow() as repos:
            cfg = await repos["configs"].get(id=config_id, joined_load=["server"])
            if not cfg:
                raise ValueError("Config not found")
            async with APIGateway(cfg.server.ip, cfg.server.port, cfg.server.api_key) as api:
                return await api.download_config(cfg.name)

    async def revoke_config(self, config_id: int) -> None:
        async with self._uow() as repos:
            cfg = await repos["configs"].get(id=config_id, joined_load=["server"])
            if not cfg:
                raise ValueError("Config not found")
            async with APIGateway(cfg.server.ip, cfg.server.port, cfg.server.api_key) as api:
                await api.revoke_client(cfg.name)
            await repos["configs"].delete(id=config_id)

    async def suspend_config(self, config_id: int) -> Config:
        async with self._uow() as repos:
            cfg = await repos["configs"].get(id=config_id, joined_load=["server"])
            if not cfg:
                raise ValueError("Config not found")
            async with APIGateway(cfg.server.ip, cfg.server.port, cfg.server.api_key) as api:
                await api.suspend_client(cfg.name)
            cfg = await repos["configs"].suspend(config_id)
            return Config.from_orm(cfg)

    async def unsuspend_config(self, config_id: int) -> Config:
        async with self._uow() as repos:
            cfg = await repos["configs"].get(id=config_id, joined_load=["server"])
            if not cfg:
                raise ValueError("Config not found")
            async with APIGateway(cfg.server.ip, cfg.server.port, cfg.server.api_key) as api:
                await api.unsuspend_client(cfg.name)
            cfg = await repos["configs"].unsuspend(config_id)
            return Config.from_orm(cfg)

    async def list_active(self, *, owner_id: int | None = None) -> Sequence[Config]:
        async with self._uow() as repos:
            configs = await repos["configs"].get_active(owner_id=owner_id)
            return [Config.from_orm(c) for c in configs]

    async def list_suspended(
        self, *, owner_id: int | None = None
    ) -> Sequence[Config]:
        async with self._uow() as repos:
            configs = await repos["configs"].get_suspended(owner_id=owner_id)
            return [Config.from_orm(c) for c in configs]

    async def list_blocked(self, server_id: int) -> Sequence[str]:
        async with self._uow() as repos:
            server = await repos["servers"].get(id=server_id)
            if not server:
                raise ValueError(f"Server {server_id} not found")
            async with APIGateway(server.ip, server.port, server.api_key) as api:
                return await api.list_blocked()
