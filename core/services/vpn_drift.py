from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from core.config import settings
from core.domain import VPNState
from core.exceptions import InvalidOperationError, ServerNotFoundError

from .api_gateway import APIGateway, ManagerClientInventory, ManagerClientState
from .config import ConfigService

DriftSeverity = Literal["info", "warning", "critical"]
DriftReason = Literal[
    "remote_missing",
    "remote_state_mismatch",
    "hub_actual_stale",
    "manager_integrity_issue",
    "unsupported_desired_state",
    "remote_only_live",
    "remote_only_inert",
    "remote_only_inconsistent",
]

_REPAIRABLE_STATES = frozenset({VPNState.ACTIVE.value, VPNState.SUSPENDED.value})


@dataclass(frozen=True, slots=True)
class VPNDriftFinding:
    server_id: int
    config_id: int | None
    name: str
    reason: DriftReason
    severity: DriftSeverity
    desired_state: str | None
    hub_actual_state: str | None
    manager_state: str | None
    repairable: bool
    details: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VPNDriftReport:
    server_id: int
    inventory_revision: str | None
    inventory_etag: str | None
    unchanged: bool
    findings: tuple[VPNDriftFinding, ...]


@dataclass(frozen=True, slots=True)
class VPNDriftRepairOperation:
    config_id: int
    operation_id: str


@dataclass(frozen=True, slots=True)
class VPNDriftRepairReport:
    server_id: int
    inventory_revision: str
    operations: tuple[VPNDriftRepairOperation, ...]
    completed: int


@dataclass(frozen=True, slots=True)
class _ServerTarget:
    id: int
    ip: str
    port: int
    api_key: str


@dataclass(frozen=True, slots=True)
class _HubConfigState:
    id: int
    name: str
    desired_state: str
    actual_state: str


class VPNDriftService:
    """Audit Manager state and stage only explicitly approved safe repairs.

    Auditing never writes. Repair is doubly opt-in: the process setting must be
    enabled and callers must supply both explicit config IDs and the exact
    Manager inventory revision they reviewed. Unknown remote clients, missing
    profiles, provisioning and revocation are never repaired automatically.
    """

    def __init__(
        self,
        uow: Callable,
        *,
        config_service: ConfigService | None = None,
        gateway_factory: Callable[..., APIGateway] | None = None,
    ) -> None:
        self._uow = uow
        self._config_service = config_service or ConfigService(uow)
        self._gateway_factory = gateway_factory or APIGateway

    async def audit_server(
        self,
        server_id: int,
        *,
        etag: str | None = None,
    ) -> VPNDriftReport:
        """Return drift findings, or an unchanged marker after Manager 304."""

        target, configs = await self._load_hub_state(server_id)
        inventory = await self._fetch_inventory(target, etag=etag)
        remote_unchanged = inventory is None
        if inventory is None:
            # A Manager ETag covers only remote inventory. Hub desired/actual
            # state was loaded fresh above and may have changed since the
            # caller cached that ETag, so a 304 can never justify an empty
            # drift report. Fetch the cached representation again without the
            # condition and compare it with current Hub state.
            inventory = await self._fetch_inventory(target)
            if inventory is None:  # pragma: no cover - unconditional HTTP invariant
                raise InvalidOperationError(
                    "Manager returned not-modified without a conditional request"
                )
        return VPNDriftReport(
            server_id=server_id,
            inventory_revision=inventory.revision,
            inventory_etag=inventory.etag,
            unchanged=remote_unchanged,
            findings=self._findings(server_id, configs, inventory),
        )

    async def repair_server(
        self,
        server_id: int,
        *,
        expected_revision: str,
        config_ids: Sequence[int],
    ) -> VPNDriftRepairReport:
        """Stage and execute reviewed active/suspended convergence operations."""

        if not settings.vpn_drift_repair_enabled:
            raise InvalidOperationError("VPN drift repair is not enabled")
        if settings.maintenance_mode:
            raise InvalidOperationError("VPN drift repair is disabled in maintenance")
        if not isinstance(expected_revision, str) or not expected_revision.strip():
            raise InvalidOperationError(
                "Expected Manager inventory revision is required"
            )

        requested = self._validated_config_ids(config_ids)
        target, configs = await self._load_hub_state(server_id)
        inventory = await self._fetch_inventory(target)
        if inventory is None:  # no conditional request was made
            raise InvalidOperationError("Manager inventory was unexpectedly unchanged")
        if inventory.revision != expected_revision:
            raise InvalidOperationError(
                "Manager inventory changed after the drift audit"
            )

        repairable = {
            finding.config_id: finding
            for finding in self._findings(server_id, configs, inventory)
            if finding.config_id is not None and finding.repairable
        }
        invalid = [config_id for config_id in requested if config_id not in repairable]
        if invalid:
            raise InvalidOperationError(
                "Requested VPN configs are not currently safe to repair: "
                + ", ".join(str(config_id) for config_id in invalid)
            )

        targets: dict[int, str] = {}
        for config_id in requested:
            desired_state = repairable[config_id].desired_state
            if desired_state not in _REPAIRABLE_STATES:  # defensive invariant
                raise InvalidOperationError(
                    "Drift finding no longer has a safe desired state"
                )
            targets[config_id] = desired_state
        planned = await self._config_service.prepare_drift_repairs(targets)
        completed = await self._config_service.execute_operations(
            tuple(planned.values())
        )
        operations = tuple(
            VPNDriftRepairOperation(config_id=config_id, operation_id=operation_id)
            for config_id, operation_id in sorted(planned.items())
        )
        return VPNDriftRepairReport(
            server_id=server_id,
            inventory_revision=inventory.revision,
            operations=operations,
            completed=completed,
        )

    @staticmethod
    def _validated_config_ids(config_ids: Sequence[int]) -> tuple[int, ...]:
        if isinstance(config_ids, (str, bytes)):
            raise InvalidOperationError("VPN drift repair config IDs are invalid")
        values: set[int] = set()
        for config_id in config_ids:
            if isinstance(config_id, bool) or not isinstance(config_id, int):
                raise InvalidOperationError("VPN drift repair config IDs are invalid")
            values.add(config_id)
        if not values:
            raise InvalidOperationError("At least one VPN config must be selected")
        if len(values) > 100:
            raise InvalidOperationError("At most 100 VPN configs can be repaired")
        return tuple(sorted(values))

    async def _load_hub_state(
        self,
        server_id: int,
    ) -> tuple[_ServerTarget, tuple[_HubConfigState, ...]]:
        if isinstance(server_id, bool) or not isinstance(server_id, int):
            raise InvalidOperationError("VPN server ID is invalid")
        async with self._uow() as repos:
            server = await repos["servers"].get(id=server_id)
            if server is None:
                raise ServerNotFoundError(f"Server {server_id} not found")
            rows = await repos["configs"].list(server_id=server_id)
            target = _ServerTarget(
                id=server.id,
                ip=server.ip,
                port=server.port,
                api_key=server.api_key,
            )
            configs = tuple(
                _HubConfigState(
                    id=row.id,
                    name=row.name,
                    desired_state=row.desired_state,
                    actual_state=row.actual_state,
                )
                for row in rows
            )
        return target, configs

    async def _fetch_inventory(
        self,
        target: _ServerTarget,
        *,
        etag: str | None = None,
    ) -> ManagerClientInventory | None:
        async with self._gateway_factory(
            target.ip,
            target.port,
            target.api_key,
        ) as gateway:
            return await gateway.get_client_inventory(etag=etag)

    @classmethod
    def _findings(
        cls,
        server_id: int,
        configs: Sequence[_HubConfigState],
        inventory: ManagerClientInventory,
    ) -> tuple[VPNDriftFinding, ...]:
        remote_by_name = {client.name: client for client in inventory.clients}
        findings: list[VPNDriftFinding] = []
        for config in configs:
            remote = remote_by_name.pop(config.name, None)
            finding = cls._config_finding(server_id, config, remote)
            if finding is not None:
                findings.append(finding)

        for name in sorted(remote_by_name):
            remote = remote_by_name[name]
            if remote.state in {"active", "suspended"}:
                reason: DriftReason = "remote_only_live"
                severity: DriftSeverity = "critical"
            elif remote.state in {"revoked", "expired"}:
                reason = "remote_only_inert"
                severity = "info"
            else:
                reason = "remote_only_inconsistent"
                severity = "warning"
            findings.append(
                VPNDriftFinding(
                    server_id=server_id,
                    config_id=None,
                    name=name,
                    reason=reason,
                    severity=severity,
                    desired_state=None,
                    hub_actual_state=None,
                    manager_state=remote.state,
                    repairable=False,
                    details=remote.issues,
                )
            )
        return tuple(findings)

    @staticmethod
    def _config_finding(
        server_id: int,
        config: _HubConfigState,
        remote: ManagerClientState | None,
    ) -> VPNDriftFinding | None:
        common = {
            "server_id": server_id,
            "config_id": config.id,
            "name": config.name,
            "desired_state": config.desired_state,
            "hub_actual_state": config.actual_state,
        }
        if remote is None:
            return VPNDriftFinding(
                **common,
                reason="remote_missing",
                severity=(
                    "info"
                    if config.desired_state == VPNState.REVOKED.value
                    else "critical"
                ),
                manager_state=None,
                repairable=False,
            )

        if not remote.manageable or remote.issues:
            details = remote.issues
            if not remote.manageable and "unmanageable_client" not in details:
                details = (*details, "unmanageable_client")
            return VPNDriftFinding(
                **common,
                reason="manager_integrity_issue",
                severity="critical",
                manager_state=remote.state,
                repairable=False,
                details=details,
            )

        if config.desired_state not in _REPAIRABLE_STATES:
            return VPNDriftFinding(
                **common,
                reason="unsupported_desired_state",
                severity="warning",
                manager_state=remote.state,
                repairable=False,
            )

        safe_lifecycle = config.actual_state in _REPAIRABLE_STATES
        if remote.state != config.desired_state:
            return VPNDriftFinding(
                **common,
                reason="remote_state_mismatch",
                severity="warning" if safe_lifecycle else "critical",
                manager_state=remote.state,
                repairable=(safe_lifecycle and remote.state in _REPAIRABLE_STATES),
            )
        if config.actual_state != config.desired_state:
            return VPNDriftFinding(
                **common,
                reason="hub_actual_stale",
                severity="warning",
                manager_state=remote.state,
                repairable=safe_lifecycle,
            )
        return None
