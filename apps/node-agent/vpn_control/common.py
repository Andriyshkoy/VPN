"""Backend boundary. No backend secrets are part of a public snapshot."""
from dataclasses import dataclass
from typing import Protocol


class BackendError(Exception):
    """A fixed non-secret code; never attach upstream response bodies."""
    def __init__(self, code: str = "backend_unavailable"):
        self.code = code
        super().__init__(code)


class BackendUnavailable(BackendError):
    pass


class BackendConflict(BackendError):
    pass


class BackendNotFound(BackendError):
    pass


@dataclass(frozen=True)
class BackendAccess:
    status: str  # active, suspended, expired, limited
    used_traffic_bytes: int = 0
    traffic_limit_bytes: int = 0
    expires_at: int | None = None  # Unix UTC, backend effective expiry
    last_seen_at: int | None = None
    expiry_precision: str = "second"
    accounting: str = "delayed_backend_counters"
    activation_days: int | None = None
    max_sessions: int | None = None  # authenticated QUIC connections per credential


class Backend(Protocol):
    def create(self, username: str, *, expires_at: int | None,
               traffic_limit_bytes: int, activation_days: int | None = None,
               max_sessions: int | None = None) -> BackendAccess: ...
    def get(self, username: str) -> BackendAccess | None: ...
    def update(self, username: str, *, expires_at: int | None,
               traffic_limit_bytes: int, activation_days: int | None = None,
               max_sessions: int | None = None) -> BackendAccess: ...
    def set_status(self, username: str, status: str) -> BackendAccess: ...
    def revoke(self, username: str) -> None: ...
    def profile(self, username: str) -> str: ...
    def health(self) -> dict: ...
