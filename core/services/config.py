from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from core.config import settings
from core.exceptions import APIConfigurationError, InvalidOperationError

from ._config_shared import _ConfigContext as _ConfigContext  # noqa: F401
from .api_gateway import APIGateway
from .config_executor import ConfigLeasedExecutorMixin
from .config_provisioning import ConfigProvisioningMixin
from .config_queries import ConfigQueriesEntitlementsMixin
from .models import Config as Config  # noqa: F401
from .node_factory import node_gateway_options
from .node_gateway import NodeAgentGateway


class ConfigService(
    ConfigProvisioningMixin,
    ConfigQueriesEntitlementsMixin,
    ConfigLeasedExecutorMixin,
):
    """Stable facade for VPN configuration application workflows.

    The focused mixins own provisioning, queries/entitlements, and leased
    execution. This facade keeps construction and adapter wiring in the legacy
    module so existing imports and ``core.services.config.APIGateway`` patches
    continue to work.
    """

    def __init__(
        self,
        uow: Callable,
        *,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = 120,
        retry_base_seconds: int = 5,
        retry_max_seconds: int = 300,
    ) -> None:
        self._uow = uow
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if lease_seconds <= 0:
            raise ValueError("VPN operation lease must be positive")
        if retry_base_seconds <= 0 or retry_max_seconds < retry_base_seconds:
            raise ValueError("Invalid VPN operation retry backoff")
        self._lease_for = timedelta(seconds=lease_seconds)
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds

    @staticmethod
    def _validate_display_name(value: str) -> str:
        if not isinstance(value, str):
            raise InvalidOperationError("Configuration display name must be text")
        value = value.strip()
        if not value or len(value) > 128:
            raise InvalidOperationError(
                "Configuration display name must contain 1 to 128 characters"
            )
        return value

    @staticmethod
    def _ensure_provisioning_enabled() -> None:
        if settings.maintenance_mode or not settings.provisioning_enabled:
            raise InvalidOperationError("VPN provisioning is temporarily disabled")

    @staticmethod
    def _create_gateway(ip: str, port: int, api_key: str):
        """Resolve the legacy patch point each time a Manager is contacted."""

        return APIGateway(ip, port, api_key)

    @staticmethod
    def _create_node_gateway(ip: str, port: int, api_key: str, *, node_id: str):
        return NodeAgentGateway(
            ip,
            port,
            api_key,
            node_id=node_id,
            **node_gateway_options(node_id, ip, port, api_key),
        )

    @staticmethod
    async def _node_context_fields(repos, cfg) -> dict:
        backend = getattr(cfg.server, "backend_type", "openvpn-manager")
        if backend == "openvpn-manager":
            return {}
        if backend != "node-agent":
            raise APIConfigurationError("Unsupported VPN backend")
        binding = await repos["vpn_access_bindings"].get_for_config(cfg.id)
        if (
            binding is None
            or binding.node_id != cfg.server.node_id
            or binding.service_id != cfg.service_id
            or binding.policy != cfg.access_policy
        ):
            raise APIConfigurationError("Node binding identity or policy mismatch")
        return {
            "backend_type": backend,
            "node_id": binding.node_id,
            "service_id": binding.service_id,
            "access_policy": dict(binding.policy),
            "remote_access_id": binding.remote_access_id,
            "provision_operation_id": binding.provision_operation_id,
        }

    def _now(self) -> datetime:
        return self._as_utc(self._clock())

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )

    @staticmethod
    def _aware(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return ConfigService._as_utc(value)
