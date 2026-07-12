from __future__ import annotations

import math
import os
import re
import socket
import threading
import time
from collections.abc import Mapping

from core.config import settings

_METRIC_PART = re.compile(r"[^a-zA-Z0-9_.-]+")
_TAG_PART = re.compile(r"[^a-zA-Z0-9_.-]+")


class StatsDClient:
    """Tiny best-effort UDP client for cross-process operational metrics.

    Metrics must never participate in application success or transaction
    semantics. DNS, socket, oversized-packet, and buffer errors are therefore
    deliberately converted into a ``False`` return value.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        host: str,
        port: int,
        service: str,
        prefix: str = "vpn_hub",
    ) -> None:
        self.enabled = enabled
        self.host = host
        self.port = port
        self.service = self._tag_value(service)
        self.prefix = self._metric_name(prefix)
        self._socket: socket.socket | None = None
        self._address: tuple | None = None
        self._pid = os.getpid()
        self._retry_resolution_after = 0.0
        self._lock = threading.Lock()

    def increment(
        self,
        metric: str,
        value: int | float = 1,
        *,
        tags: Mapping[str, object] | None = None,
    ) -> bool:
        return self._emit(metric, value, "c", tags)

    def gauge(
        self,
        metric: str,
        value: int | float,
        *,
        tags: Mapping[str, object] | None = None,
    ) -> bool:
        return self._emit(metric, value, "g", tags)

    def timing(
        self,
        metric: str,
        duration_seconds: float,
        *,
        tags: Mapping[str, object] | None = None,
    ) -> bool:
        duration_ms = max(0.0, duration_seconds) * 1000.0
        return self._emit(metric, duration_ms, "ms", tags)

    def close(self) -> None:
        with self._lock:
            if self._socket is not None:
                self._socket.close()
            self._socket = None
            self._address = None

    def _emit(
        self,
        metric: str,
        value: int | float,
        metric_type: str,
        tags: Mapping[str, object] | None,
    ) -> bool:
        if not self.enabled:
            return False
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(numeric):
            return False

        metric_name = f"{self.prefix}.{self._metric_name(metric)}"
        rendered_value = format(numeric, ".12g")
        rendered_tags = {"service": self.service}
        if tags:
            rendered_tags.update(
                {
                    self._tag_value(key): self._tag_value(value)
                    for key, value in tags.items()
                }
            )
        tag_suffix = ",".join(
            f"{key}:{rendered_tags[key]}" for key in sorted(rendered_tags)
        )
        payload = f"{metric_name}:{rendered_value}|{metric_type}|#{tag_suffix}"
        return self._send(payload)

    def _send(self, payload: str) -> bool:
        encoded = payload.encode("ascii", errors="replace")
        # Stay below a normal Ethernet MTU so UDP metrics are not fragmented.
        if len(encoded) > 1400:
            return False

        with self._lock:
            try:
                sock, address = self._socket_and_address()
                sock.sendto(encoded, address)
            except (BlockingIOError, IndexError, OSError):
                return False
        return True

    def _socket_and_address(self) -> tuple[socket.socket, tuple]:
        pid = os.getpid()
        if pid != self._pid:
            if self._socket is not None:
                self._socket.close()
            self._socket = None
            self._address = None
            self._pid = pid

        if self._socket is not None and self._address is not None:
            return self._socket, self._address

        now = time.monotonic()
        if now < self._retry_resolution_after:
            raise OSError("StatsD address resolution is in backoff")
        try:
            addresses = socket.getaddrinfo(
                self.host,
                self.port,
                type=socket.SOCK_DGRAM,
            )
            family, socktype, proto, _, address = addresses[0]
            sock = socket.socket(family, socktype, proto)
            sock.setblocking(False)
        except OSError:
            self._retry_resolution_after = now + 5.0
            raise

        self._socket = sock
        self._address = address
        return sock, address

    @staticmethod
    def _metric_name(value: object) -> str:
        normalized = _METRIC_PART.sub("_", str(value).strip())
        return normalized.strip("._-") or "unknown"

    @staticmethod
    def _tag_value(value: object) -> str:
        normalized = _TAG_PART.sub("_", str(value).strip())
        return (normalized.strip("._-") or "unknown")[:64]


statsd = StatsDClient(
    enabled=settings.observability_enabled,
    host=settings.statsd_host,
    port=settings.statsd_port,
    service=settings.vpn_hub_service,
)


def observe_manager_request(
    *,
    operation: str,
    method: str,
    outcome: str,
    status_code: int | None,
    attempts: int,
    duration_seconds: float,
) -> None:
    try:
        tags = {
            "operation": operation,
            "method": method.lower(),
            "outcome": outcome,
            "status_code": status_code if status_code is not None else "none",
        }
        statsd.increment("manager.requests", tags=tags)
        statsd.timing("manager.request_duration", duration_seconds, tags=tags)
        if attempts > 1:
            statsd.increment("manager.retries", attempts - 1, tags=tags)
    except Exception:
        return


def observe_background_job(name: str, outcome: str, duration_seconds: float) -> None:
    try:
        tags = {"job": name, "outcome": outcome}
        statsd.increment("background_jobs", tags=tags)
        statsd.timing("background_job_duration", duration_seconds, tags=tags)
    except Exception:
        return


def observe_outbox_publish(outcome: str, value: int = 1) -> None:
    try:
        statsd.increment(
            "notification_outbox.publish", value, tags={"outcome": outcome}
        )
    except Exception:
        return
