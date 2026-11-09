"""Unprivileged Hysteria adapter; privileged access stays behind a Unix socket."""
from dataclasses import fields
import json
import re
import socket

from .common import BackendAccess, BackendConflict, BackendNotFound, BackendUnavailable


USERNAME = re.compile(r"vc_[a-f0-9]{24}\Z")
MAX_MESSAGE = 65536


class HysteriaBackend:
    def __init__(self, socket_path="/run/vpn-control-hys/bridge.sock", timeout=20):
        self.socket_path = str(socket_path)
        self.timeout = timeout

    def _call(self, op, username=None, **kwargs):
        if username is not None and not USERNAME.fullmatch(username):
            raise BackendConflict("backend_username_not_owned")
        request = {"version": 1, "op": op, **kwargs}
        if username is not None:
            request["username"] = username
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.timeout)
                client.connect(self.socket_path)
                client.sendall(json.dumps(request, separators=(",", ":")).encode() + b"\n")
                data = bytearray()
                while b"\n" not in data and len(data) <= MAX_MESSAGE:
                    block = client.recv(min(4096, MAX_MESSAGE + 1 - len(data)))
                    if not block:
                        break
                    data.extend(block)
            if len(data) > MAX_MESSAGE or b"\n" not in data:
                raise ValueError("invalid_reply")
            reply = json.loads(bytes(data).split(b"\n", 1)[0])
            if not isinstance(reply, dict) or type(reply.get("ok")) is not bool:
                raise ValueError("invalid_reply")
        except (OSError, ValueError, TypeError):
            raise BackendUnavailable("hysteria_bridge_unavailable") from None
        if not reply["ok"]:
            error = reply.get("error", {})
            kind = error.get("kind") if isinstance(error, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,80}", code):
                raise BackendUnavailable("hysteria_bridge_unavailable")
            cls = {"conflict": BackendConflict, "not_found": BackendNotFound}.get(kind, BackendUnavailable)
            raise cls(code)
        return reply.get("result")

    @staticmethod
    def _snapshot(result):
        if result is None:
            return None
        try:
            keys = {item.name for item in fields(BackendAccess)}
            if not isinstance(result, dict) or set(result) - keys:
                raise ValueError("invalid_snapshot")
            snapshot = BackendAccess(**result)
            if snapshot.status not in {"active", "suspended", "expired", "limited"}:
                raise ValueError("invalid_status")
            for value in (snapshot.used_traffic_bytes, snapshot.traffic_limit_bytes):
                if type(value) is not int or value < 0:
                    raise ValueError("invalid_counter")
            return snapshot
        except (TypeError, ValueError):
            raise BackendUnavailable("hysteria_bridge_invalid_snapshot") from None

    @staticmethod
    def _policy(expires_at, traffic_limit_bytes, activation_days):
        if expires_at is not None:
            raise BackendConflict("hysteria_exact_expiry_unsupported")
        if type(traffic_limit_bytes) is not int or not 0 <= traffic_limit_bytes <= 2**63 - 1:
            raise BackendConflict("invalid_traffic_limit")
        if activation_days is not None and (type(activation_days) is not int or not 1 <= activation_days <= 3660):
            raise BackendConflict("invalid_activation_days")
        return {"expires_at": None, "traffic_limit_bytes": traffic_limit_bytes,
                "activation_days": activation_days}

    def create(self, username, *, expires_at, traffic_limit_bytes, activation_days=None):
        policy = self._policy(expires_at, traffic_limit_bytes, activation_days)
        return self._snapshot(self._call("create", username, **policy))

    def get(self, username):
        return self._snapshot(self._call("get", username))

    def update(self, username, *, expires_at, traffic_limit_bytes, activation_days=None):
        policy = self._policy(expires_at, traffic_limit_bytes, activation_days)
        return self._snapshot(self._call("update", username, **policy))

    def set_status(self, username, status):
        if status not in {"active", "suspended"}:
            raise BackendConflict("invalid_access_status")
        return self._snapshot(self._call("set_status", username, status=status))

    def revoke(self, username):
        self._call("revoke", username)

    def profile(self, username):
        result = self._call("profile", username)
        if not isinstance(result, str) or not result.startswith("hysteria2://") or len(result) > 8192:
            raise BackendUnavailable("hysteria_bridge_invalid_profile")
        return result

    def health(self):
        result = self._call("health")
        if not isinstance(result, dict) or type(result.get("ready")) is not bool:
            raise BackendUnavailable("hysteria_bridge_invalid_health")
        return result
