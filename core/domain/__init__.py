"""Backend domain primitives shared by application services."""

from .fleet import AdminActionStatus, ServerLifecycleState
from .telegram import TelegramUpdateStatus
from .vpn import VPNOperationKind, VPNOperationStatus, VPNState

__all__ = [
    "TelegramUpdateStatus",
    "AdminActionStatus",
    "ServerLifecycleState",
    "VPNOperationKind",
    "VPNOperationStatus",
    "VPNState",
]
