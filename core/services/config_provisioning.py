from __future__ import annotations

import uuid

from core.domain import VPNOperationKind, VPNState
from core.exceptions import (
    ConfigNotFoundError,
    InvalidOperationError,
    ServerNotFoundError,
    UserNotFoundError,
)

from ._config_shared import _ConfigContext
from .fleet_placement import (
    is_managed_config,
    latest_server_status,
    placement_decision,
)
from .models import Config
from .node_policy import normalize_node_policy


class ConfigProvisioningMixin:
    """Stage and execute creation of a new VPN configuration."""

    async def create_config(
        self,
        *,
        server_id: int,
        owner_id: int,
        name: str,
        display_name: str,
        use_password: bool = False,
        service_id: str | None = None,
        access_policy: dict | None = None,
        operation_id: str | None = None,
    ) -> Config:
        try:
            operation_id = (
                str(uuid.UUID(operation_id)) if operation_id else str(uuid.uuid4())
            )
        except (ValueError, TypeError, AttributeError) as exc:
            raise InvalidOperationError("Invalid VPN operation ID") from exc
        request = None
        if service_id is not None:
            request = {
                "server_id": server_id,
                "owner_id": owner_id,
                "name": name,
                "display_name": self._validate_display_name(display_name),
                "service_id": service_id,
                "access_policy": normalize_node_policy(service_id, access_policy),
            }
        async with self._uow() as repos:
            previous = await repos["vpn_operations"].get(operation_id=operation_id)
            if previous is not None:
                if (
                    request is None
                    or previous.kind != VPNOperationKind.PROVISION.value
                    or previous.payload.get("node_request") != request
                    or previous.config_id is None
                ):
                    raise InvalidOperationError("Provision operation ID conflicts")
                config_id = previous.config_id
            else:
                context = await self.prepare_config(
                    repos=repos,
                    operation_id=operation_id,
                    server_id=server_id,
                    owner_id=owner_id,
                    name=name,
                    display_name=display_name,
                    use_password=use_password,
                    service_id=service_id,
                    access_policy=access_policy,
                )
                config_id = context.config_id
        await self._execute(operation_id)
        result = await self.get(config_id)
        if result is None:
            raise ConfigNotFoundError("Provisioned configuration no longer exists")
        return result

    async def prepare_config(
        self,
        *,
        repos,
        operation_id: str,
        server_id: int,
        owner_id: int,
        name: str,
        display_name: str,
        use_password: bool = False,
        operation_payload: dict | None = None,
        service_id: str | None = None,
        access_policy: dict | None = None,
    ) -> _ConfigContext:
        """Stage provision in the caller's transaction without doing network I/O."""

        self._ensure_provisioning_enabled()
        display_name = self._validate_display_name(display_name)
        if not isinstance(name, str) or not name or len(name) > 128:
            raise InvalidOperationError("Invalid internal configuration name")
        try:
            operation_id = str(uuid.UUID(operation_id))
        except (AttributeError, TypeError, ValueError) as exc:
            raise InvalidOperationError("Invalid VPN operation ID") from exc

        # Serialise provisioning with Manager endpoint mutation/deletion.
        server = await repos["servers"].get_for_update(server_id)
        if not server:
            raise ServerNotFoundError(f"Server {server_id} not found")
        existing_configs = await repos["configs"].list(server_id=server_id)
        managed_configs = sum(is_managed_config(config) for config in existing_configs)
        latest_status = await latest_server_status(
            repos["servers"].session,
            server.id,
        )
        if not placement_decision(server, managed_configs, latest_status).allowed:
            raise InvalidOperationError("VPN server is not accepting new configs")
        user = await repos["users"].get(id=owner_id)
        if not user:
            raise UserNotFoundError(f"User {owner_id} not found")

        payload = dict(operation_payload or {})
        payload["use_password"] = bool(use_password)
        backend_type = getattr(server, "backend_type", "openvpn-manager")
        policy = None
        if backend_type == "node-agent":
            if use_password:
                raise InvalidOperationError(
                    "Node services do not use OpenVPN passwords"
                )
            if not getattr(server, "node_id", None):
                raise InvalidOperationError("Node identity is required")
            policy = normalize_node_policy(service_id, access_policy)
            payload["node_request"] = {
                "server_id": server_id,
                "owner_id": owner_id,
                "name": name,
                "display_name": display_name,
                "service_id": service_id,
                "access_policy": policy,
            }
        elif service_id is not None or access_policy is not None:
            raise InvalidOperationError("Legacy Manager does not accept node policy")
        cfg = await repos["configs"].create(
            server_id,
            owner_id,
            name,
            display_name,
            desired_state=VPNState.ACTIVE.value,
            actual_state=VPNState.PROVISIONING.value,
            operation_id=operation_id,
            service_id=service_id,
            access_policy=policy,
        )
        if backend_type == "node-agent":
            await repos["vpn_access_bindings"].create(
                config_id=cfg.id,
                server_id=server.id,
                node_id=server.node_id,
                service_id=service_id,
                provision_operation_id=operation_id,
                policy=policy,
            )
        await repos["vpn_operations"].create(
            operation_id=operation_id,
            config_id=cfg.id,
            config_name=cfg.name,
            server_id=server.id,
            owner_id=owner_id,
            kind=VPNOperationKind.PROVISION.value,
            payload=payload,
            next_attempt_at=self._now(),
        )
        return _ConfigContext(
            config_id=cfg.id,
            name=cfg.name,
            owner_id=owner_id,
            server_id=server.id,
            server_ip=server.ip,
            server_port=server.port,
            server_api_key=server.api_key,
            operation_id=operation_id,
            kind=VPNOperationKind.PROVISION.value,
            payload=payload,
            backend_type=backend_type,
            node_id=getattr(server, "node_id", None),
            service_id=service_id,
            access_policy=policy,
            provision_operation_id=operation_id if policy is not None else None,
        )

    async def execute_prepared(self, context: _ConfigContext) -> Config:
        """Execute a provision intent after its surrounding transaction commits."""

        await self._execute(context.operation_id)
        result = await self.get(context.config_id)
        if result is None:  # pragma: no cover - defensive invariant
            raise ConfigNotFoundError(f"Config with ID {context.config_id} not found")
        return result
