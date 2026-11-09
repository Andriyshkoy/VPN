from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select

from core.db.models import VPNAccessBinding

from .base import BaseRepo


class VPNAccessBindingRepo(BaseRepo[VPNAccessBinding]):
    model = VPNAccessBinding

    async def create(
        self,
        *,
        config_id: int,
        server_id: int,
        node_id: str,
        service_id: str,
        provision_operation_id: str,
        policy: dict,
        config_ref: int | None = None,
    ) -> VPNAccessBinding:
        if service_id not in {"hysteria", "vless-reality"}:
            raise ValueError("Invalid node service")
        operation_id = str(UUID(provision_operation_id))
        existing = await self.get_for_config(config_id)
        snapshot = json.loads(json.dumps(policy, sort_keys=True))
        if existing is not None:
            if (
                existing.server_id != server_id
                or existing.node_id != node_id
                or existing.service_id != service_id
                or existing.policy != snapshot
                or existing.provision_operation_id != operation_id
                or existing.config_ref
                != (config_ref if config_ref is not None else config_id)
            ):
                raise ValueError(
                    "Node binding identity and provision policy are immutable"
                )
            return existing
        return await self.add(
            self.model(
                config_id=config_id,
                config_ref=config_ref if config_ref is not None else config_id,
                server_id=server_id,
                node_id=node_id,
                service_id=service_id,
                provision_operation_id=operation_id,
                policy=snapshot,
                observed_state="provisioning",
            )
        )

    async def get_for_config(self, config_id: int) -> VPNAccessBinding | None:
        return await self.session.scalar(
            select(self.model)
            .where(self.model.config_id == config_id)
            .with_for_update()
        )

    async def get_for_config_ref(self, config_ref: int) -> VPNAccessBinding | None:
        return await self.session.scalar(
            select(self.model)
            .where(self.model.config_ref == config_ref)
            .with_for_update()
        )

    async def attach_remote(
        self, config_id: int, remote_access_id: str, *, observed_state: str = "active"
    ) -> VPNAccessBinding:
        """Caller must hold the operation lease/fencing lock in this transaction."""
        binding = await self.get_for_config(config_id)
        if binding is None or binding.revoked_at is not None:
            raise ValueError("Node binding is absent or revoked")
        remote_id = str(UUID(remote_access_id))
        if binding.remote_access_id not in (None, remote_id):
            raise ValueError("Remote access identity is immutable")
        binding.remote_access_id = remote_id
        binding.observed_state = observed_state
        await self.session.flush()
        return binding

    async def update_observed(self, config_id: int, state: str) -> VPNAccessBinding:
        binding = await self.get_for_config(config_id)
        if binding is None or (binding.revoked_at is not None and state != "revoked"):
            raise ValueError("Node binding is absent or permanently revoked")
        binding.observed_state = state
        await self.session.flush()
        return binding

    async def tombstone(
        self, config_id: int, *, now: datetime | None = None
    ) -> VPNAccessBinding:
        binding = await self.get_for_config(config_id)
        if binding is None:
            raise ValueError("Node binding is absent")
        binding.revoked_at = binding.revoked_at or now or datetime.now(timezone.utc)
        binding.observed_state = "revoked"
        await self.session.flush()
        return binding
