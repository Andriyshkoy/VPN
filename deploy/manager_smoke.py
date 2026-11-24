"""Read-only production smoke for non-retired Manager mTLS endpoints."""

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

    managers = []
    live_servers = []
    for server in servers:
        backend = getattr(server, "backend_type", None)
        if backend not in {"openvpn-manager", "node-agent"}:
            raise RuntimeError(
                f"Manager smoke found an unsupported backend for {_server_label(server)}"
            )
        if getattr(server, "lifecycle_state", None) == "retired":
            continue
        live_servers.append(server)
        if backend == "openvpn-manager":
            managers.append(server)
        # Node readiness/capabilities require a separate check. Sending
        # Manager mTLS requests to that API would test the wrong protocol.
    if not live_servers:
        raise RuntimeError(
            "Manager smoke test requires at least one non-retired server"
        )
    if not managers:
        print(
            "manager_smoke=skipped reason=no_nonretired_openvpn_managers "
            "node_health=not_checked"
        )
        return

    seen_instance_ids: set[str] = set()
    for server in managers:
        await verify_manager(server, seen_instance_ids=seen_instance_ids)

    print("manager_smoke=ok")


if __name__ == "__main__":
    asyncio.run(main())
