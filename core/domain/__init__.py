"""Backend domain primitives shared by application services."""

from .vpn import VPNOperationKind, VPNOperationStatus, VPNState

__all__ = ["VPNOperationKind", "VPNOperationStatus", "VPNState"]
