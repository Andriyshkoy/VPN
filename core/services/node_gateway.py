"""Typed client for the private, versioned VPN node-agent API.

This adapter has no OpenVPN/PKI compatibility facade. Profile export is explicit
and secrets never form part of access metadata or exception messages.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import ssl
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from core.exceptions import (
    APIAuthenticationError,
    APIConfigurationError,
    APIConflictError,
    APIHTTPError,
    APINotFoundError,
    APIProtocolError,
    APIRequestRejectedError,
    APIServerError,
    APITLSConfigurationError,
    APITransportError,
)
from core.services.node_endpoints import (
    canonical_literal_origin,
    validate_wireguard_origin,
)

_SERVICES = frozenset({"hysteria", "vless-reality"})
_STATES = frozenset(
    {"active", "suspended", "revoked", "expired", "limited", "provisioning", "failed"}
)
_DESIRED_STATES = frozenset({"active", "suspended", "revoked"})
_RETRYABLE = frozenset({408, 425, 429, 500, 502, 503, 504})
_POLICY_FIELDS = frozenset({"traffic_limit_bytes", "expires_at", "activation_days"})
_EFFECTIVE_FIELDS = frozenset(
    {
        "status",
        "used_traffic_bytes",
        "traffic_limit_bytes",
        "expires_at",
        "last_seen_at",
        "expiry_precision",
        "accounting",
        "activation_days",
    }
)
_MAX_RESPONSE_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class NodeAccess:
    id: str
    node_id: str
    service_id: str
    status: str
    desired_status: str
    settings: dict[str, Any]
    effective: dict[str, Any] | None
    revision: int
    operation_pending: bool
    control_plane_verified: bool


@dataclass(frozen=True, slots=True)
class NodeProfile:
    access_id: str
    service_id: str
    uri: str = field(repr=False)
    format: str = "uri"
    contains_secret: bool = True


@dataclass(frozen=True, slots=True)
class NodeReadiness:
    node_id: str
    ready: bool
    database_ready: bool
    services: dict[str, bool]
    scope: str = "control_plane"
    data_plane_verified: bool = False


@dataclass(frozen=True, slots=True)
class NodeService:
    id: str
    node_id: str
    endpoint: dict[str, Any]
    capabilities: dict[str, Any]


@dataclass(frozen=True, slots=True)
class NodeUsage:
    access_id: str
    used_traffic_bytes: int
    traffic_limit_bytes: int
    accounting: str
    observed_at: str
    realtime: bool = False
    billing_authoritative: bool = False


def _protocol_error() -> APIProtocolError:
    return APIProtocolError("VPN node returned an invalid response")


def _integer(value: Any, *, maximum: int = 2**63 - 1) -> bool:
    return type(value) is int and 0 <= value <= maximum


def _expiry(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise _protocol_error()
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        raise _protocol_error() from None


class NodeAgentGateway:
    """Verified HTTPS, or an explicit loopback/registered WireGuard origin."""

    def __init__(
        self,
        ip: str,
        port: int,
        api_key: str,
        *,
        node_id: str,
        tls_enabled: bool | None = None,
        trusted_wireguard_base_url: str | None = None,
        ca_cert_path: str | None = None,
        client_cert_path: str | None = None,
        client_key_path: str | None = None,
        timeout: float = 20.0,
        retries: int = 2,
        backoff: float = 0.25,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not isinstance(ip, str) or not ip or len(ip) > 253:
            raise APIConfigurationError("VPN node host is invalid")
        if any(x.isspace() for x in ip) or any(x in ip for x in "/?#@"):
            raise APIConfigurationError("VPN node host is invalid")
        host = ip
        raw = ip[1:-1] if ip.startswith("[") and ip.endswith("]") else ip
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", ip):
                raise APIConfigurationError("VPN node host is invalid") from None
            if any(
                not label
                or len(label) > 63
                or label.startswith("-")
                or label.endswith("-")
                for label in ip.split(".")
            ):
                raise APIConfigurationError("VPN node host is invalid")
            loopback = ip.lower() == "localhost"
        else:
            loopback = address.is_loopback
            host = f"[{address}]" if address.version == 6 else str(address)
        if type(port) is not int or not 1 <= port <= 65535:
            raise APIConfigurationError("VPN node port is invalid")
        if (
            not isinstance(api_key, str)
            or not 32 <= len(api_key) <= 512
            or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", api_key)
        ):
            raise APIConfigurationError("VPN node credential is invalid")
        if not isinstance(node_id, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9-]{0,62}", node_id
        ):
            raise APIConfigurationError("VPN node identity is invalid")
        trusted_wireguard = False
        if trusted_wireguard_base_url is not None:
            trusted_origin = validate_wireguard_origin(trusted_wireguard_base_url)
            if canonical_literal_origin(raw, port) != trusted_origin:
                raise APIConfigurationError("VPN node endpoint registration is invalid")
            trusted_wireguard = True
        if tls_enabled is None:
            tls_enabled = not (loopback or trusted_wireguard)
        if type(tls_enabled) is not bool or (
            not tls_enabled and not (loopback or trusted_wireguard)
        ):
            raise APIConfigurationError(
                "VPN node HTTP requires a registered protected endpoint"
            )
        if trusted_wireguard and tls_enabled:
            raise APIConfigurationError(
                "VPN node endpoint registration conflicts with TLS"
            )
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 120
        ):
            raise APIConfigurationError("VPN node timeout is invalid")
        if type(retries) is not int or not 0 <= retries <= 5:
            raise APIConfigurationError("VPN node retries are invalid")
        if (
            isinstance(backoff, bool)
            or not isinstance(backoff, (int, float))
            or not 0 <= backoff <= 5
        ):
            raise APIConfigurationError("VPN node backoff is invalid")
        self.node_id = node_id
        self._headers = {"Authorization": "Bearer " + api_key}
        self._timeout, self._retries, self._backoff = timeout, retries, backoff
        self._sleep, self._transport = sleep, transport
        self._base_url = f"{'https' if tls_enabled else 'http'}://{host}:{port}"
        self._tls_context: ssl.SSLContext | None = None
        if bool(client_cert_path) != bool(client_key_path):
            raise APITLSConfigurationError("VPN node client identity is incomplete")
        if not tls_enabled and any((ca_cert_path, client_cert_path, client_key_path)):
            raise APITLSConfigurationError("VPN node TLS material requires HTTPS")
        if tls_enabled:
            try:
                self._tls_context = ssl.create_default_context(
                    cafile=ca_cert_path or None
                )
                if client_cert_path and client_key_path:
                    self._tls_context.load_cert_chain(client_cert_path, client_key_path)
            except (OSError, ssl.SSLError, ValueError, TypeError):
                raise APITLSConfigurationError(
                    "VPN node TLS material is unreadable or invalid"
                ) from None
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> NodeAgentGateway:
        if self._client is not None:
            await self._client.aclose()
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=self._timeout,
            verify=self._tls_context or True,
            trust_env=False,
            follow_redirects=False,
            transport=self._transport,
        )
        return self

    async def __aexit__(self, *_args: Any) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    @staticmethod
    def _operation_id(operation_id: str) -> str:
        try:
            return str(UUID(operation_id))
        except (TypeError, ValueError, AttributeError):
            raise APIConfigurationError(
                "VPN node operation ID must be a UUID"
            ) from None

    @staticmethod
    def _policy(service_id: str, policy: Mapping[str, Any]) -> dict[str, Any]:
        if (
            not isinstance(service_id, str)
            or service_id not in _SERVICES
            or not isinstance(policy, Mapping)
            or set(policy) - _POLICY_FIELDS
        ):
            raise APIConfigurationError("VPN node access policy is invalid")
        result = {
            "traffic_limit_bytes": 0,
            "expires_at": None,
            "activation_days": None,
            **policy,
        }
        days = result["activation_days"]
        if not _integer(result["traffic_limit_bytes"]) or not _expiry(
            result["expires_at"]
        ):
            raise APIConfigurationError("VPN node access policy is invalid")
        if days is not None and (type(days) is not int or not 1 <= days <= 3660):
            raise APIConfigurationError("VPN node access policy is invalid")
        if (service_id == "hysteria" and result["expires_at"] is not None) or (
            service_id == "vless-reality" and days is not None
        ):
            raise APIConfigurationError("VPN node expiry policy does not match service")
        return result

    @staticmethod
    def _http_error(status: int, attempt: int) -> Exception:
        message = "VPN node request was rejected"
        if status in (401, 403):
            return APIAuthenticationError(message, status_code=status, attempts=attempt)
        if status == 404:
            return APINotFoundError(message, status_code=status, attempts=attempt)
        if status == 409:
            return APIConflictError(message, status_code=status, attempts=attempt)
        if status in _RETRYABLE:
            return APIServerError(
                "VPN node temporarily unavailable",
                status_code=status,
                attempts=attempt,
                retryable=True,
            )
        if status == 422:
            return APIRequestRejectedError(
                message, status_code=status, attempts=attempt
            )
        return APIHTTPError(message, status_code=status, attempts=attempt)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        operation_id: str | None = None,
        health: bool = False,
    ) -> dict[str, Any]:
        if self._client is None:
            raise APIConfigurationError("VPN node gateway requires an async context")
        headers = {}
        if method != "GET":
            if operation_id is None:
                raise APIConfigurationError(
                    "VPN node mutation requires an operation ID"
                )
            headers["Idempotency-Key"] = self._operation_id(operation_id)
        encoded = (
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
            if body is not None
            else None
        )
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(1, self._retries + 2):
            try:
                response = await self._client.request(
                    method, path, content=encoded, headers=headers
                )
            except httpx.RequestError:
                if attempt <= self._retries:
                    await self._sleep(min(self._backoff * 2 ** (attempt - 1), 5))
                    continue
                raise APITransportError(
                    "VPN node transport failed", attempts=attempt
                ) from None
            if 200 <= response.status_code < 300 or (
                health and response.status_code == 503
            ):
                if len(response.content) > _MAX_RESPONSE_BYTES:
                    raise _protocol_error()
                try:
                    value = response.json()
                except (ValueError, UnicodeError):
                    raise _protocol_error() from None
                if not isinstance(value, dict):
                    raise _protocol_error()
                return value
            if response.status_code in _RETRYABLE and attempt <= self._retries:
                await self._sleep(min(self._backoff * 2 ** (attempt - 1), 5))
                continue
            raise self._http_error(response.status_code, attempt)
        raise RuntimeError("Unreachable retry state")

    def _access(
        self,
        payload: dict[str, Any],
        *,
        expected_id: str | None = None,
        expected_service: str | None = None,
    ) -> NodeAccess:
        value = payload.get("access")
        if not isinstance(value, dict):
            raise _protocol_error()
        access_id = _uuid(value.get("id"))
        service = value.get("service_id")
        if (
            value.get("node_id") != self.node_id
            or not isinstance(service, str)
            or service not in _SERVICES
            or (expected_service is not None and service != expected_service)
            or (expected_id is not None and access_id != expected_id)
        ):
            raise _protocol_error()
        status, desired = value.get("status"), value.get("desired_status")
        if (
            not isinstance(status, str)
            or not isinstance(desired, str)
            or status not in _STATES
            or desired not in _DESIRED_STATES
        ):
            raise _protocol_error()
        try:
            policy = self._policy(service, value.get("settings"))
        except APIConfigurationError:
            raise _protocol_error() from None
        effective = value.get("effective")
        if effective is not None:
            if (
                not isinstance(effective, dict)
                or set(effective) - _EFFECTIVE_FIELDS
                or not isinstance(effective.get("status"), str)
                or effective.get("status")
                not in _DESIRED_STATES | {"expired", "limited"}
            ):
                raise _protocol_error()
            if not all(
                _integer(effective.get(k, 0))
                for k in ("used_traffic_bytes", "traffic_limit_bytes")
            ) or not all(
                _expiry(effective.get(k)) for k in ("expires_at", "last_seen_at")
            ):
                raise _protocol_error()
            if (
                not isinstance(effective.get("expiry_precision", "second"), str)
                or effective.get("expiry_precision", "second")
                not in {
                    "second",
                    "day",
                }
                or not isinstance(effective.get("accounting", ""), str)
            ):
                raise _protocol_error()
            if effective.get("activation_days") is not None and (
                type(effective["activation_days"]) is not int
                or not 1 <= effective["activation_days"] <= 3660
            ):
                raise _protocol_error()
            effective = dict(effective)
        if (
            not _integer(value.get("revision"))
            or type(value.get("operation_pending")) is not bool
            or type(value.get("control_plane_verified")) is not bool
            or value.get("data_plane_verified") is not False
        ):
            raise _protocol_error()
        return NodeAccess(
            access_id,
            self.node_id,
            service,
            status,
            desired,
            policy,
            effective,
            value["revision"],
            value["operation_pending"],
            value["control_plane_verified"],
        )

    async def create_access(
        self,
        *,
        service_id: str,
        owner_ref: str,
        policy: Mapping[str, Any],
        operation_id: str,
    ) -> NodeAccess:
        if not isinstance(owner_ref, str) or not re.fullmatch(
            r"[a-zA-Z0-9_@.:/-]{1,128}", owner_ref
        ):
            raise APIConfigurationError("VPN node owner reference is invalid")
        body = {
            "service_id": service_id,
            "node_id": self.node_id,
            "owner_ref": owner_ref,
            **self._policy(service_id, policy),
        }
        payload = await self._request(
            "POST", "/v1/accesses", body=body, operation_id=operation_id
        )
        result = self._access(payload, expected_service=service_id)
        if payload["access"].get("owner_ref") != owner_ref:
            raise _protocol_error()
        expected_policy = self._policy(service_id, policy)
        for key in ("traffic_limit_bytes", "activation_days"):
            if result.settings[key] != expected_policy[key]:
                raise _protocol_error()
        expected_expiry, observed_expiry = (
            expected_policy["expires_at"],
            result.settings["expires_at"],
        )
        if (expected_expiry is None) != (observed_expiry is None):
            raise _protocol_error()
        if expected_expiry is not None and int(
            datetime.fromisoformat(expected_expiry.replace("Z", "+00:00")).timestamp()
        ) != int(
            datetime.fromisoformat(observed_expiry.replace("Z", "+00:00")).timestamp()
        ):
            raise _protocol_error()
        return result

    async def get_access(self, access_id: str) -> NodeAccess:
        identifier = _uuid(access_id)
        return self._access(
            await self._request("GET", f"/v1/accesses/{identifier}"),
            expected_id=identifier,
        )

    async def _transition(
        self, access_id: str, operation_id: str, action: str
    ) -> NodeAccess:
        identifier = _uuid(access_id)
        path = f"/v1/accesses/{identifier}"
        method = "DELETE" if action == "revoke" else "POST"
        if action != "revoke":
            path += "/" + action
        return self._access(
            await self._request(method, path, operation_id=operation_id),
            expected_id=identifier,
        )

    async def suspend_access(self, access_id: str, *, operation_id: str) -> NodeAccess:
        return await self._transition(access_id, operation_id, "suspend")

    async def resume_access(self, access_id: str, *, operation_id: str) -> NodeAccess:
        return await self._transition(access_id, operation_id, "resume")

    async def revoke_access(self, access_id: str, *, operation_id: str) -> NodeAccess:
        return await self._transition(access_id, operation_id, "revoke")

    async def export_profile(self, access_id: str) -> NodeProfile:
        identifier = _uuid(access_id)
        value = await self._request("GET", f"/v1/accesses/{identifier}/profile")
        uri, service = value.get("uri"), value.get("service_id")
        if (
            value.get("access_id") != identifier
            or not isinstance(service, str)
            or service not in _SERVICES
            or value.get("format") != "uri"
            or value.get("contains_secret") is not True
            or not isinstance(uri, str)
            or len(uri) > 16384
            or any(c in uri for c in "\r\n")
        ):
            raise _protocol_error()
        try:
            parsed = urlsplit(uri)
            port = parsed.port
        except ValueError:
            raise _protocol_error() from None
        if (
            parsed.scheme
            not in ({"hy2", "hysteria2"} if service == "hysteria" else {"vless"})
            or not parsed.hostname
            or not parsed.username
            or port is None
        ):
            raise _protocol_error()
        return NodeProfile(identifier, service, uri)

    async def get_usage(self, access_id: str) -> NodeUsage:
        identifier = _uuid(access_id)
        value = await self._request("GET", f"/v1/accesses/{identifier}/usage")
        if (
            value.get("access_id") != identifier
            or not _integer(value.get("used_traffic_bytes"))
            or not _integer(value.get("traffic_limit_bytes"))
            or value.get("realtime") is not False
            or value.get("billing_authoritative") is not False
            or not isinstance(value.get("accounting"), str)
            or not _expiry(value.get("observed_at"))
            or value.get("observed_at") is None
        ):
            raise _protocol_error()
        return NodeUsage(
            identifier,
            value["used_traffic_bytes"],
            value["traffic_limit_bytes"],
            value["accounting"],
            value["observed_at"],
        )

    @staticmethod
    def _capabilities(value: dict[str, Any]) -> dict[str, Any]:
        known = {
            "fixed_expiry",
            "activation_days",
            "expiry_precision",
            "traffic_limit_bytes",
            "unlimited_traffic",
            "unlimited_expiry",
        }
        result = {key: item for key, item in value.items() if key in known}
        if any(
            type(item) is not bool
            for key, item in result.items()
            if key != "expiry_precision"
        ) or result.get("expiry_precision") not in (None, "second", "day"):
            raise _protocol_error()
        return result

    async def catalog(self) -> tuple[NodeService, ...]:
        value = await self._request("GET", "/v1/services")
        services = value.get("services")
        if not isinstance(services, list) or len(services) != len(_SERVICES):
            raise _protocol_error()
        result = []
        for entry in services:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("id"), str)
                or entry.get("id") not in _SERVICES
                or entry.get("node_id") != self.node_id
                or not isinstance(entry.get("endpoint"), dict)
                or not isinstance(entry.get("capabilities"), dict)
            ):
                raise _protocol_error()
            endpoint = entry["endpoint"]
            if (
                not isinstance(endpoint.get("host"), str)
                or not endpoint["host"]
                or not _integer(endpoint.get("port"), maximum=65535)
                or endpoint["port"] == 0
                or not isinstance(endpoint.get("transport"), str)
                or endpoint.get("transport") not in {"tcp", "udp"}
            ):
                raise _protocol_error()
            result.append(
                NodeService(
                    entry["id"],
                    self.node_id,
                    {k: endpoint[k] for k in ("host", "port", "transport")},
                    self._capabilities(entry["capabilities"]),
                )
            )
        if {x.id for x in result} != _SERVICES:
            raise _protocol_error()
        return tuple(result)

    async def ready(self) -> NodeReadiness:
        identity = await self._request("GET", "/v1/nodes")
        nodes = identity.get("nodes")
        if (
            not isinstance(nodes, list)
            or len(nodes) != 1
            or not isinstance(nodes[0], dict)
            or nodes[0].get("id") != self.node_id
            or not isinstance(nodes[0].get("services"), list)
            or not all(isinstance(s, str) for s in nodes[0]["services"])
            or len(nodes[0]["services"]) != len(_SERVICES)
            or set(nodes[0]["services"]) != _SERVICES
        ):
            raise _protocol_error()
        value = await self._request("GET", "/readyz", health=True)
        services = value.get("services")
        if (
            type(value.get("ready")) is not bool
            or type(value.get("database_ready")) is not bool
            or not isinstance(services, dict)
            or set(services) != _SERVICES
        ):
            raise _protocol_error()
        checks = {}
        for service, entry in services.items():
            if not isinstance(entry, dict) or type(entry.get("ready")) is not bool:
                raise _protocol_error()
            checks[service] = entry["ready"]
        if value["ready"] != (value["database_ready"] and all(checks.values())):
            raise _protocol_error()
        return NodeReadiness(
            self.node_id, value["ready"], value["database_ready"], checks
        )
