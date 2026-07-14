from __future__ import annotations

from decimal import Decimal
from typing import Callable, Sequence

from core.domain import ServerLifecycleState
from core.exceptions import InvalidOperationError

from .fleet_placement import (
    is_managed_config,
    latest_server_status,
    placement_decision,
)
from .models import Server


class ServerService:
    """Operations on VPN **servers**."""

    def __init__(self, uow: Callable) -> None:
        self._uow = uow

    async def get(self, server_id: int) -> Server | None:
        async with self._uow() as repos:
            server = await repos["servers"].get(id=server_id)
            return Server.from_orm(server) if server else None

    async def list(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        location: str | None = None,
        host: str | None = None,
        available_only: bool = False,
    ) -> Sequence[Server]:
        """Return servers filtered by the provided parameters."""
        filters: dict[str, object] = {}
        if location is not None:
            filters["location"] = location
        if host is not None:
            filters["host"] = host

        async with self._uow() as repos:
            # Availability filtering must happen before pagination, because a
            # disabled or full node must never appear as a provision target.
            fetch_limit = None if available_only else limit
            fetch_offset = 0 if available_only else offset
            servers = await repos["servers"].list(
                limit=fetch_limit, offset=fetch_offset, **filters
            )
            if available_only:
                available = []
                for server in servers:
                    rows = await repos["configs"].list(server_id=server.id)
                    managed = sum(is_managed_config(row) for row in rows)
                    latest = await latest_server_status(
                        repos["servers"].session, server.id
                    )
                    if self._accepts_new_config(server, managed, latest):
                        available.append(server)
                servers = available[offset : offset + limit if limit else None]
            return [Server.from_orm(s) for s in servers]

    async def create(
        self,
        name: str,
        ip: str,
        port: int,
        host: str,
        location: str,
        api_key: str,
        cost: int,
        lifecycle_state: str = ServerLifecycleState.DISABLED.value,
        accepts_new_configs: bool = False,
        max_configs: int | None = None,
        capacity_reserve: int = 0,
        placement_weight: Decimal = Decimal("1"),
        provider: str | None = None,
        public_endpoint: str | None = None,
        manager_instance_id: str | None = None,
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
                lifecycle_state=lifecycle_state,
                accepts_new_configs=accepts_new_configs,
                max_configs=max_configs,
                capacity_reserve=capacity_reserve,
                placement_weight=placement_weight,
                provider=provider,
                public_endpoint=public_endpoint,
                manager_instance_id=manager_instance_id,
            )
            return Server.from_orm(server)

    async def delete(self, server_id: int) -> bool:
        async with self._uow() as repos:
            server = await repos["servers"].get_for_update(server_id)
            if server is None:
                return False
            configs = await repos["configs"].list(server_id=server_id, limit=1)
            if configs:
                # A server must be drained and its remote clients revoked
                # before the local control-plane record can disappear.
                return False
            deleted = await repos["servers"].delete(id=server_id)
            return bool(deleted)

    async def update(self, server_id: int, **fields) -> Server | None:
        """Update a server and return the updated object, or ``None`` if missing."""
        async with self._uow() as repos:
            current = await repos["servers"].get_for_update(server_id)
            if current is None:
                return None
            # Every legacy write participates in the same optimistic version
            # fence as the v1 fleet API.
            fields.pop("version", None)
            endpoint_fields = {"ip", "port", "api_key"}
            endpoint_changed = any(
                key in fields and fields[key] != getattr(current, key)
                for key in endpoint_fields
            )
            if endpoint_changed:
                configs = await repos["configs"].list(server_id=server_id)
                if any(is_managed_config(config) for config in configs):
                    raise InvalidOperationError(
                        "Drain all VPN configs before changing the Manager endpoint"
                    )
                fields["lifecycle_state"] = ServerLifecycleState.DISABLED.value
                fields["accepts_new_configs"] = False
                fields["manager_instance_id"] = None
            fields["version"] = getattr(current, "version", 1) + 1
            srv = await repos["servers"].update(server_id, **fields)
            return Server.from_orm(srv) if srv else None

    @staticmethod
    def _accepts_new_config(server, managed_configs: int, latest_status) -> bool:
        return placement_decision(server, managed_configs, latest_status).allowed

    async def accepts_new_config(self, server_id: int) -> bool:
        async with self._uow() as repos:
            server = await repos["servers"].get(id=server_id)
            if server is None:
                return False
            configs = await repos["configs"].list(server_id=server_id)
            managed = sum(is_managed_config(config) for config in configs)
            latest = await latest_server_status(repos["servers"].session, server.id)
            return self._accepts_new_config(server, managed, latest)
