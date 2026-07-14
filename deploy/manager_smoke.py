"""Read-only production smoke test for the Manager mTLS control plane."""

from __future__ import annotations

import asyncio
from typing import Any

from core.db.unit_of_work import uow
from core.services.api_gateway import APIGateway


def _server_label(server: Any) -> str:
    server_id = getattr(server, "id", None)
    return f"server {server_id}" if server_id is not None else "configured server"


async def verify_manager(server: Any, *, seen_instance_ids: set[str]) -> None:
    """Require the authenticated Manager 1.3 fleet contract for one node."""

    async with APIGateway(server, timeout=10, retries=1) as gateway:
        status = await gateway.get_status()
        await gateway.get_client_inventory()

    label = _server_label(server)
    if not status.readiness.ready:
        raise RuntimeError(f"Manager readiness failed for {label}")
    if status.data_plane.status != "up":
        raise RuntimeError(f"OpenVPN data plane is not up for {label}")

    expected_instance_id = getattr(server, "manager_instance_id", None)
    if expected_instance_id and status.instance_id != expected_instance_id:
        raise RuntimeError(f"Manager instance identity changed for {label}")
    if status.instance_id in seen_instance_ids:
        raise RuntimeError(
            "Multiple server records returned one Manager instance identity"
        )
    seen_instance_ids.add(status.instance_id)


async def main() -> None:
    async with uow() as repos:
        servers = list(await repos["servers"].list())

    if not servers:
        raise RuntimeError("Manager smoke test requires at least one server")

    seen_instance_ids: set[str] = set()
    for server in servers:
        await verify_manager(server, seen_instance_ids=seen_instance_ids)

    print("manager_smoke=ok")


if __name__ == "__main__":
    asyncio.run(main())
