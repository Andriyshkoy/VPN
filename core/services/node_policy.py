"""Persistable technical policy; product prices and paid periods live elsewhere."""

from __future__ import annotations

from datetime import datetime, timezone

from core.exceptions import InvalidOperationError

NODE_SERVICES = frozenset({"hysteria", "vless-reality"})
NODE_POLICY_FIELDS = frozenset({"traffic_limit_bytes", "expires_at", "activation_days"})


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
    return {
        "traffic_limit_bytes": quota,
        "expires_at": expiry,
        "activation_days": days,
    }
