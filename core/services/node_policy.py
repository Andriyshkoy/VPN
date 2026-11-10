"""Persistable technical policy; hourly product prices live elsewhere."""

from __future__ import annotations

from datetime import datetime, timezone

from core.exceptions import InvalidOperationError

NODE_SERVICES = frozenset({"hysteria", "vless-reality"})
NODE_POLICY_FIELDS = frozenset(
    {"traffic_limit_bytes", "expires_at", "activation_days", "max_sessions"}
)


def normalize_node_policy(service_id: str, policy: dict | None) -> dict:
    """Return one immutable JSON-compatible body for every operation replay."""

    if service_id not in NODE_SERVICES:
        raise InvalidOperationError("Unsupported node service")
    if policy is None:
        policy = {}
    if not isinstance(policy, dict) or set(policy) - NODE_POLICY_FIELDS:
        raise InvalidOperationError("Invalid node access policy")
    quota = policy.get("traffic_limit_bytes", 0)
    if type(quota) is not int or not 0 <= quota <= 2**63 - 1:
        raise InvalidOperationError("Invalid node traffic quota")
    days = policy.get("activation_days")
    if days is not None and (type(days) is not int or not 1 <= days <= 3660):
        raise InvalidOperationError("Invalid node activation period")
    expiry = policy.get("expires_at")
    if expiry is not None:
        try:
            if isinstance(expiry, str):
                expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if not isinstance(expiry, datetime) or expiry.utcoffset() is None:
                raise ValueError("timezone required")
            expiry = expiry.astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError, OverflowError) as exc:
            raise InvalidOperationError(
                "Node expiry requires an aware timestamp"
            ) from exc
    if service_id == "hysteria" and expiry is not None:
        raise InvalidOperationError("Hysteria requires activation_days")
    if service_id == "vless-reality" and days is not None:
        raise InvalidOperationError("VLESS requires exact expires_at")
    sessions = policy.get("max_sessions")
    if sessions is not None and (type(sessions) is not int or not 1 <= sessions <= 128):
        raise InvalidOperationError("Invalid concurrent session limit")
    if service_id != "hysteria" and sessions is not None:
        raise InvalidOperationError("Session limits require Hysteria")
    result = {
        "traffic_limit_bytes": quota,
        "expires_at": expiry,
        "activation_days": days,
    }
    # Do not rewrite existing immutable policy snapshots just because the new
    # optional field was added. New product grants explicitly set None or 1.
    if "max_sessions" in policy:
        result["max_sessions"] = sessions
    return result
