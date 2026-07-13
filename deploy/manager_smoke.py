"""Read-only production smoke test for the Manager mTLS control plane."""

from __future__ import annotations

import asyncio

from core.db.unit_of_work import uow
from core.services.api_gateway import APIGateway


async def main() -> None:
    async with uow() as repos:
        servers = list(await repos["servers"].list())

    if not servers:
        raise RuntimeError("Manager smoke test requires at least one server")

    for server in servers:
        async with APIGateway(server, timeout=10, retries=1) as gateway:
            await gateway.get_client_inventory()

    print("manager_smoke=ok")


if __name__ == "__main__":
    asyncio.run(main())
