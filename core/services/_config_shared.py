from __future__ import annotations

from dataclasses import dataclass

from core.domain import VPNOperationKind, VPNOperationStatus, VPNState

_NON_TERMINAL_STATUSES = {
    VPNOperationStatus.PENDING.value,
    VPNOperationStatus.RUNNING.value,
    VPNOperationStatus.FAILED.value,
}
_ACTIVATING_KINDS = {
    VPNOperationKind.PROVISION.value,
    VPNOperationKind.UNSUSPEND.value,
}
_TARGET_BY_KIND = {
    VPNOperationKind.PROVISION.value: VPNState.ACTIVE.value,
    VPNOperationKind.SUSPEND.value: VPNState.SUSPENDED.value,
    VPNOperationKind.UNSUSPEND.value: VPNState.ACTIVE.value,
    VPNOperationKind.REVOKE.value: VPNState.REVOKED.value,
}
_KIND_BY_TARGET = {
    VPNState.ACTIVE.value: VPNOperationKind.UNSUSPEND.value,
    VPNState.SUSPENDED.value: VPNOperationKind.SUSPEND.value,
    VPNState.REVOKED.value: VPNOperationKind.REVOKE.value,
}


@dataclass(frozen=True)
class _ConfigContext:
    """Immutable data needed to perform one Manager operation."""

    config_id: int
    name: str
    owner_id: int
    server_id: int
    server_ip: str
    server_port: int
    server_api_key: str
    operation_id: str
    kind: str
    payload: dict
    lease_token: str | None = None
    attempts: int = 0
    backend_type: str = "openvpn-manager"
    node_id: str | None = None
    service_id: str | None = None
    access_policy: dict | None = None
    remote_access_id: str | None = None
    provision_operation_id: str | None = None
