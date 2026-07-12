"""Backend domain primitives shared by application services."""

from .telegram import TelegramUpdateStatus
from .vpn import VPNOperationKind, VPNOperationStatus, VPNState

__all__ = [
    "TelegramUpdateStatus",
    "VPNOperationKind",
    "VPNOperationStatus",
    "VPNState",
]
