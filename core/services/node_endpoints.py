"""Private, operator-owned registration of exact WireGuard HTTP endpoints.

The catalog declares that the listed origins are carried by the managed
WireGuard overlay. A private IP address alone never grants a plaintext exception.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import re
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from core.exceptions import APIConfigurationError

_MAX_CATALOG_BYTES = 65_536
_NODE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*")


def _invalid() -> APIConfigurationError:
    # Never interpolate file paths, parsed input, tokens or operating-system text.
    return APIConfigurationError("VPN node endpoint registration is invalid")


def canonical_literal_origin(ip: str, port: int) -> str:
    """Return one exact HTTP origin for a literal IPv4/IPv6 endpoint."""
    if (
        not isinstance(ip, str)
        or "%" in ip
        or type(port) is not int
        or not 1 <= port <= 65535
    ):
        raise _invalid()
    raw = ip[1:-1] if ip.startswith("[") and ip.endswith("]") else ip
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        raise _invalid() from None
    if address.is_unspecified or address.is_multicast:
        raise _invalid()
    host = f"[{address}]" if address.version == 6 else str(address)
    return f"http://{host}:{port}"


def validate_wireguard_origin(value: str) -> str:
    """A registration has no DNS name, credentials, path, query or fragment."""
    if not isinstance(value, str) or len(value) > 256:
        raise _invalid()
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or not parsed.hostname
            or parsed.port is None
        ):
            raise _invalid()
        canonical = canonical_literal_origin(parsed.hostname, parsed.port)
    except (ValueError, TypeError):
        raise _invalid() from None
    if canonical != value:
        raise _invalid()
    return canonical


def _valid_token(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 32 <= len(value) <= 512
        and _TOKEN.fullmatch(value) is not None
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def load_node_endpoints(
    catalogpath: str | os.PathLike[str],
) -> dict[str, dict[str, str]]:
    """Read a regular no-follow catalog owned privately by root or this UID."""
    try:
        path = Path(catalogpath)
        descriptor = os.open(
            path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        )
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid not in (0, os.geteuid())
                or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
            ):
                raise _invalid()
            raw = stream.read(_MAX_CATALOG_BYTES + 1)
        if len(raw) > _MAX_CATALOG_BYTES:
            raise _invalid()
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (OSError, ValueError, TypeError, UnicodeError):
        raise _invalid() from None
    if not isinstance(value, dict) or not value or len(value) > 256:
        raise _invalid()
    for node_id, entry in value.items():
        if (
            not isinstance(node_id, str)
            or _NODE_ID.fullmatch(node_id) is None
            or not isinstance(entry, dict)
            or set(entry) != {"base_url", "token", "transport"}
            or entry["transport"] != "wireguard-http"
            or not _valid_token(entry["token"])
        ):
            raise _invalid()
        validate_wireguard_origin(entry["base_url"])
    return value


def resolve_node_endpoint(
    node_id: str,
    ip: str,
    port: int,
    api_key: str,
    catalogpath: str | os.PathLike[str] | None,
) -> dict[str, Any]:
    """Grant HTTP only after the DB identity, origin and secret match the file.

    An absent catalog preserves the gateway's normal HTTPS/loopback policy.
    Once a catalog is configured, an unknown or changed registration fails
    before any request rather than silently changing the authentication route.
    """
    if catalogpath is None or catalogpath == "":
        return {}
    if not isinstance(node_id, str) or not _valid_token(api_key):
        raise _invalid()
    entries = load_node_endpoints(catalogpath)
    entry = entries.get(node_id)
    if (
        entry is None
        or canonical_literal_origin(ip, port) != entry["base_url"]
        or not hmac.compare_digest(api_key, entry["token"])
    ):
        raise _invalid()
    return {"trusted_wireguard_base_url": entry["base_url"], "tls_enabled": False}
