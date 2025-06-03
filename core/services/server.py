from __future__ import annotations

from typing import Any, Mapping, Sequence

from core.db.models import Server


class ServerService:
    """Business helpers for **VPN servers** (but not configs)."""

    def __init__(self, repos: Mapping[str, Any]) -> None:
        self._servers = repos["servers"]

    async def get(self, server_id: int) -> Server | None:
        return await self._servers.get(id=server_id)

    async def list(self, **filters) -> Sequence[Server]:
        return await self._servers.list(**filters)

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
        return await self._servers.create(
            name=name,
            ip=ip,
            port=port,
            host=host,
            location=location,
            api_key=api_key,
            cost=cost
        )
