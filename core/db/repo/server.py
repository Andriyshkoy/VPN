from typing import Sequence

from sqlalchemy import select, update

from core.db.models import Server

from .base import BaseRepo


class ServerRepo(BaseRepo[Server]):
    model = Server

    async def search_by_name(self, query: str, limit: int = 20) -> Sequence[Server]:
        """
        Search servers by name using a case-insensitive partial match.

        Args:
            query: The search term to look for in server names
            limit: Maximum number of results to return (default: 20)

        Returns:
            Sequence of matching server objects
        """
        stmt = (
            select(self.model)
            .where(self.model.name.ilike(f"%{query}%"))
            .limit(limit)
        )
        servers = await self.session.scalars(stmt)
        return servers.all()

    async def search_by_location(self, location: str, limit: int = 20) -> Sequence[Server]:
        """
        Search servers by location using a case-insensitive partial match.

        Args:
            location: The location term to search for
            limit: Maximum number of results to return (default: 20)

        Returns:
            Sequence of matching server objects
        """
        stmt = (
            select(self.model)
            .where(self.model.location.ilike(f"%{location}%"))
            .limit(limit)
        )
        servers = await self.session.scalars(stmt)
        return servers.all()

    async def create(self, name: str, ip: str, port: int,
                     host: str, location: str, api_key: str, cost: int) -> Server:
        """
        Create a new server entry.

        Args:
            name: Name of the server
            ip: IP address of the server
            port: Port number of the server API

            host: Hostname of the server
            cost: Monthly cost of the server
            location: Geographical location of the server
            api_key: API key for the server

        Returns:
            The created Server object
        """
        server = self.model(
            name=name,
            ip=ip,
            port=port,
            host=host,
            monthly_cost=cost,
            location=location,
            api_key=api_key,
        )
        return await self.add(server)

    async def update(self, server_id: int, **kwargs) -> Server:
        """
        Update an existing server entry.

        Args:
            server_id: ID of the server to update
            **kwargs: Attributes to update

        Returns:
            The updated Server object
        """
        stmt = (
            update(self.model)
            .where(self.model.id == server_id)
            .values(**kwargs)
            .returning(self.model)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.scalar_one_or_none()

    async def delete(self, **filters) -> int:
        """Delete a server and cascade delete its VPN configs."""
        server = await self.get(**filters, joined_load=["vpn_configs"])
        if not server:
            return 0
        await self.session.delete(server)
        await self.session.flush()
        return 1
