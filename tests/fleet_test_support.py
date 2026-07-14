from __future__ import annotations

from datetime import datetime, timezone

from core.db.models import VPNServerStatus
from core.db.unit_of_work import uow

READY_MANAGER_INSTANCE_ID = "56c1ab62-0c42-4f03-83c6-4c8e6c43e29b"


async def mark_server_ready(server_id: int) -> None:
    """Explicitly activate a test server after a healthy Manager observation."""

    async with uow() as repos:
        server = await repos["servers"].get(id=server_id)
        if server is None:
            raise AssertionError(f"Test server {server_id} does not exist")
        server.lifecycle_state = "active"
        server.accepts_new_configs = True
        server.manager_instance_id = READY_MANAGER_INSTANCE_ID
        repos["servers"].session.add(
            VPNServerStatus(
                server_id=server_id,
                kind="status",
                success=True,
                manager_instance_id=READY_MANAGER_INSTANCE_ID,
                collected_at=datetime.now(timezone.utc),
                snapshot={
                    "readiness": {"ready": True},
                    "data_plane": {"status": "up"},
                },
            )
        )
