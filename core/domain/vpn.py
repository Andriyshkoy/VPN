from enum import StrEnum


class VPNState(StrEnum):
    """Desired and observed state of a VPN client on its manager."""

    PROVISIONING = "provisioning"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"
    FAILED = "failed"


class VPNOperationKind(StrEnum):
    PROVISION = "provision"
    SUSPEND = "suspend"
    UNSUSPEND = "unsuspend"
    REVOKE = "revoke"


class VPNOperationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    EXHAUSTED = "exhausted"
