from __future__ import annotations

from typing import Callable, Sequence

from .models import Server


class ServerService:
    """Operations on VPN **servers**."""

    def __init__(self, uow: Callable) -> None:
        self._uow = uow

    async def get(self, server_id: int) -> Server | None:
        async with self._uow() as repos:
            server = await repos["servers"].get(id=server_id)
            return Server.from_orm(server) if server else None

    async def list(self, **filters) -> Sequence[Server]:
        async with self._uow() as repos:
            servers = await repos["servers"].list(**filters)
            return [Server.from_orm(s) for s in servers]

    async def create(
        self,
        name: str,
        ip: str,
        port: int,
        host: str,
        location: str,
        api_key: str,
        cost: int
    ) -> Server:
        async with self._uow() as repos:
            server = await repos["servers"].create(
                name=name,
                ip=ip,
                port=port,
                host=host,
                location=location,
                api_key=api_key,
                cost=cost,
            )
            return Server.from_orm(server)

    async def delete(self, server_id: int) -> bool:
        async with self._uow() as repos:
            deleted = await repos["servers"].delete(id=server_id)
            return bool(deleted)

    async def update(self, server_id: int, **fields) -> Server | None:
        """Update a server and return the updated object, or ``None`` if missing."""
        async with self._uow() as repos:
            srv = await repos["servers"].update(server_id, **fields)
            return Server.from_orm(srv) if srv else None
