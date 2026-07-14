from enum import StrEnum


class ServerLifecycleState(StrEnum):
    """Administrative lifecycle of a VPN server."""

    ACTIVE = "active"
    DRAINING = "draining"
    DISABLED = "disabled"
    RETIRED = "retired"


class AdminActionStatus(StrEnum):
    """Durable state of an administrative fleet operation."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


__all__ = ["AdminActionStatus", "ServerLifecycleState"]
