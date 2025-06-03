from typing import Sequence

from sqlalchemy import select

from core.db.models import Server

from .base import BaseRepo


class ServerRepo(BaseRepo[Server]):
    model = Server

    async def search_by_name(self, query: str, limit: int = 20) -> Sequence[Server]:
        stmt = (
            select(Server)
            .where(Server.name.ilike(f"%{query}%"))
            .limit(limit)
        )
        servers = await self.session.scalars(stmt)
        return servers.all()

    async def search_by_location(self, location: str, limit: int = 20) -> Sequence[Server]:
        stmt = (
            select(Server)
            .where(Server.location.ilike(f"%{location}%"))
            .limit(limit)
        )
        servers = await self.session.scalars(stmt)
        return servers.all()

    async def get_or_create(self, name: str, **kwargs) -> Server:
        server = await self.get(name=name)
        if server:
            return server
        return await self.add(Server(name=name, **kwargs))
