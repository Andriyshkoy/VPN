from __future__ import annotations

import asyncio
import random
import ssl
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from ipaddress import IPv6Address
from types import TracebackType
from typing import Any, Literal, cast
from urllib.parse import quote

import httpx

from core.config import settings
from core.exceptions import (
    APIAuthenticationError,
    APIConfigurationError,
    APIConflictError,
    APIHTTPError,
    APINotFoundError,
    APIProtocolError,
    APIRateLimitError,
    APIRequestRejectedError,
    APIRetryableResponseError,
    APIServerError,
    APITLSConfigurationError,
    APITransportError,
)
from core.observability.statsd import observe_manager_request

_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_IDEMPOTENCY_HEADER = "Idempotency-Key"

Sleep = Callable[[float], Awaitable[None]]
Random = Callable[[], float]

ManagerObservedState = Literal[
    "active",
    "suspended",
    "revoked",
    "expired",
    "incomplete",
    "orphaned",
    "unknown",
]
ManagerCertificateStatus = Literal[
    "valid",
    "revoked",
    "expired",
    "unknown",
    "missing",
]

_MANAGER_STATES = frozenset(
    {
        "active",
        "suspended",
        "revoked",
        "expired",
        "incomplete",
        "orphaned",
        "unknown",
    }
)
_CERTIFICATE_STATUSES = frozenset({"valid", "revoked", "expired", "unknown", "missing"})


@dataclass(frozen=True, slots=True)
class ManagerClientState:
    """Typed, non-secret state returned by OpenVPN Manager 1.2+."""

    name: str
    state: ManagerObservedState
    certificate_status: ManagerCertificateStatus
    index_statuses: tuple[str, ...]
    suspended: bool
    config_present: bool
    config_complete: bool
    certificate_present: bool
    private_key_present: bool
    manageable: bool
    issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManagerClientInventory:
    """One content-addressed Manager inventory snapshot."""

    revision: str
    count: int
    clients: tuple[ManagerClientState, ...]
    etag: str | None = None


class APIGateway:
    """Thin async wrapper around the remote VPN‑management REST API."""

    def __init__(
        self,
        ip: str | object,
        port: int | None = None,
        api_key: str | None = None,
        *,
        timeout: float = 20.0,
        retries: int = 3,
        backoff: float = 1.0,
        jitter: float = 0.2,
        max_backoff: float = 30.0,
        tls_enabled: bool | None = None,
        mtls_required: bool | None = None,
        tls_port: int | None = None,
        ca_cert_path: str | None = None,
        client_cert_path: str | None = None,
        client_key_path: str | None = None,
        sleep: Sleep = asyncio.sleep,
        random_value: Random = random.random,
    ) -> None:
        """
        Initialize the API gateway with server details.
        Args:
            ip: IP address or object with `ip` and `port` attributes
            port: Port number of the API (if not provided, will be taken from `ip`)
            api_key: API key for authentication
            timeout: Request timeout in seconds
            retries: Number of retry attempts on failure
            backoff: Initial exponential backoff in seconds
            jitter: Maximum proportional jitter added to each delay
            max_backoff: Upper bound for a retry delay in seconds
            tls_enabled: Use HTTPS instead of the legacy HTTP transport
            mtls_required: Fail closed unless CA and Hub client identity exist
            tls_port: Optional HTTPS port overriding the stored legacy port
            ca_cert_path: Optional private CA bundle for Manager verification
            client_cert_path: Optional Hub certificate for mutual TLS
            client_key_path: Private key paired with `client_cert_path`
            sleep: Injectable async sleep function (primarily for testing)
            random_value: Injectable source of a value in the [0, 1] range
        """
        if not isinstance(ip, str):
            server = ip
            ip = getattr(server, "ip", None)
            if port is None:
                port = getattr(server, "port", None)
            if api_key is None:
                api_key = getattr(server, "api_key", None)

        if tls_enabled is None:
            tls_enabled = settings.vpn_manager_tls_enabled
        if not isinstance(tls_enabled, bool):
            raise APIConfigurationError("VPN Manager TLS flag must be boolean")
        if mtls_required is None:
            mtls_required = settings.vpn_manager_mtls_required
        if not isinstance(mtls_required, bool):
            raise APITLSConfigurationError(
                "VPN Manager mTLS requirement flag must be boolean"
            )
        if tls_port is None:
            tls_port = settings.vpn_manager_tls_port
        if tls_enabled and tls_port is not None:
            port = tls_port

        host = self._validate_host(ip)
        self._validate_options(
            port=port,
            api_key=api_key,
            timeout=timeout,
            retries=retries,
            backoff=backoff,
            jitter=jitter,
            max_backoff=max_backoff,
        )
        validated_port = cast(int, port)
        validated_api_key = cast(str, api_key)

        if ca_cert_path is None:
            ca_cert_path = settings.vpn_manager_ca_cert_path
        if client_cert_path is None:
            client_cert_path = settings.vpn_manager_client_cert_path
        if client_key_path is None:
            client_key_path = settings.vpn_manager_client_key_path
        self._tls_context = self._build_tls_context(
            enabled=tls_enabled,
            mtls_required=mtls_required,
            ca_cert_path=ca_cert_path,
            client_cert_path=client_cert_path,
            client_key_path=client_key_path,
        )

        scheme = "https" if tls_enabled else "http"
        self._base_url = f"{scheme}://{host}:{validated_port}"
        self._headers = {"X-API-Key": validated_api_key}
        self._timeout = timeout
        self._retries = retries
        self._backoff = backoff
        self._jitter = jitter
        self._max_backoff = max_backoff
        self._sleep = sleep
        self._random_value = random_value
        self._client: httpx.AsyncClient | None = None

    @staticmethod
    def _build_tls_context(
        *,
        enabled: bool,
        mtls_required: bool,
        ca_cert_path: str | None,
        client_cert_path: str | None,
        client_key_path: str | None,
    ) -> ssl.SSLContext | None:
        if mtls_required and not enabled:
            raise APITLSConfigurationError(
                "VPN Manager mTLS cannot be required while TLS is disabled"
            )
        if not enabled:
            return None
        paths = (ca_cert_path, client_cert_path, client_key_path)
        if any(value is not None and not isinstance(value, str) for value in paths):
            raise APITLSConfigurationError("VPN Manager TLS paths must be strings")

        ca_path = ca_cert_path.strip() if ca_cert_path else None
        cert_path = client_cert_path.strip() if client_cert_path else None
        key_path = client_key_path.strip() if client_key_path else None
        if mtls_required and not all((ca_path, cert_path, key_path)):
            raise APITLSConfigurationError(
                "VPN Manager mTLS requires CA, client certificate, and key material"
            )
        if bool(cert_path) != bool(key_path):
            raise APITLSConfigurationError(
                "VPN Manager client certificate and key must be configured together"
            )
        try:
            context = ssl.create_default_context(cafile=ca_path)
            if cert_path and key_path:
                context.load_cert_chain(certfile=cert_path, keyfile=key_path)
        except (OSError, ssl.SSLError, ValueError) as exc:
            raise APITLSConfigurationError(
                "VPN Manager TLS material is invalid or unreadable"
            ) from exc
        return context

    @staticmethod
    def _validate_host(ip: object) -> str:
        if not isinstance(ip, str) or not ip.strip():
            raise APIConfigurationError("VPN Manager host must be a non-empty string")

        host = ip.strip()
        if any(char.isspace() for char in host) or any(
            marker in host for marker in ("://", "/", "?", "#", "@")
        ):
            raise APIConfigurationError("VPN Manager host has an invalid format")

        if "[" in host or "]" in host:
            if not (host.startswith("[") and host.endswith("]")):
                raise APIConfigurationError("VPN Manager host has an invalid format")
            try:
                IPv6Address(host[1:-1])
            except ValueError as exc:
                raise APIConfigurationError(
                    "VPN Manager host has an invalid IPv6 address"
                ) from exc
            return host

        if ":" in host:
            try:
                IPv6Address(host)
            except ValueError as exc:
                raise APIConfigurationError(
                    "VPN Manager host must not include a port"
                ) from exc
            return f"[{host}]"

        return host

    @staticmethod
    def _validate_options(
        *,
        port: object,
        api_key: object,
        timeout: object,
        retries: object,
        backoff: object,
        jitter: object,
        max_backoff: object,
    ) -> None:
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
        ):
            raise APIConfigurationError("VPN Manager port must be between 1 and 65535")
        if (
            not isinstance(api_key, str)
            or not api_key.strip()
            or "\r" in api_key
            or "\n" in api_key
        ):
            raise APIConfigurationError(
                "VPN Manager API key must be a non-empty string"
            )
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
        ):
            raise APIConfigurationError("VPN Manager timeout must be greater than zero")
        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
            raise APIConfigurationError(
                "VPN Manager retries must be a non-negative integer"
            )
        if (
            isinstance(backoff, bool)
            or not isinstance(backoff, (int, float))
            or backoff < 0
        ):
            raise APIConfigurationError("VPN Manager backoff must be non-negative")
        if (
            isinstance(jitter, bool)
            or not isinstance(jitter, (int, float))
            or not 0 <= jitter <= 1
        ):
            raise APIConfigurationError("VPN Manager jitter must be between 0 and 1")
        if (
            isinstance(max_backoff, bool)
            or not isinstance(max_backoff, (int, float))
            or max_backoff < 0
        ):
            raise APIConfigurationError("VPN Manager max_backoff must be non-negative")

    async def __aenter__(self) -> "APIGateway":
        await self._create_client()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        client = self._client
        self._client = None
        if client:
            await client.aclose()

    async def _create_client(self) -> None:
        if self._client:
            await self._client.aclose()
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=self._timeout,
            verify=self._tls_context or True,
            trust_env=False,
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        accepted_status_codes: Sequence[int] = (),
        **kwargs: Any,
    ) -> httpx.Response:
        client = self._client
        if client is None:
            raise APIConfigurationError(
                "APIGateway must be entered as an async context manager"
            )

        method = method.upper()
        can_retry = self._can_retry(method, kwargs.get("headers"))
        max_attempts = self._retries + 1 if can_retry else 1
        operation = self._operation_label(method, url)
        started_at = time.monotonic()
        attempt = 0
        outcome = "internal_error"
        status_code: int | None = None

        try:
            for attempt in range(1, max_attempts + 1):
                try:
                    response = await client.request(method, url, **kwargs)
                except httpx.RequestError as exc:
                    if attempt >= max_attempts:
                        outcome = "transport_error"
                        raise APITransportError(
                            f"VPN Manager transport failure after {attempt} attempt(s)",
                            attempts=attempt,
                        ) from exc
                    await self._wait_before_retry(attempt)
                    continue

                status_code = response.status_code
                if (
                    200 <= response.status_code < 300
                    or response.status_code in accepted_status_codes
                ):
                    outcome = "success"
                    return response

                status_is_retryable = response.status_code in _RETRYABLE_STATUS_CODES
                if status_is_retryable and can_retry and attempt < max_attempts:
                    await self._wait_before_retry(attempt)
                    continue

                outcome = self._status_outcome(response.status_code)
                raise self._http_error(
                    response.status_code,
                    attempts=attempt,
                    retryable=status_is_retryable and can_retry,
                )
        finally:
            observe_manager_request(
                operation=operation,
                method=method,
                outcome=outcome,
                status_code=status_code,
                attempts=max(1, attempt),
                duration_seconds=time.monotonic() - started_at,
            )

        raise RuntimeError("unreachable")  # pragma: no cover

    @staticmethod
    def _operation_label(method: str, url: str) -> str:
        path = url.split("?", 1)[0].rstrip("/") or "/"
        if path == "/clients/blocked":
            return "list_blocked"
        if path == "/clients" and method == "GET":
            return "client_inventory"
        if path == "/clients" and method == "POST":
            return "create_client"
        if path.endswith("/state") and method == "GET":
            return "client_state"
        if path.endswith("/config") and method == "GET":
            return "download_config"
        if path.endswith("/suspend") and method == "POST":
            return "suspend_client"
        if path.endswith("/unsuspend") and method == "POST":
            return "unsuspend_client"
        if path.startswith("/clients/") and method == "DELETE":
            return "revoke_client"
        return "unknown"

    @staticmethod
    def _status_outcome(status_code: int) -> str:
        if status_code in {401, 403}:
            return "authentication_error"
        if status_code == 429:
            return "rate_limited"
        if status_code >= 500:
            return "server_error"
        return "rejected"

    @staticmethod
    def _can_retry(method: str, headers: object) -> bool:
        if method in _IDEMPOTENT_METHODS:
            return True
        if not isinstance(headers, Mapping):
            return False
        return any(
            isinstance(key, str) and key.lower() == _IDEMPOTENCY_HEADER.lower()
            for key in headers
        )

    async def _wait_before_retry(self, retry_number: int) -> None:
        base_delay = self._backoff * (2 ** (retry_number - 1))
        random_value = min(1.0, max(0.0, float(self._random_value())))
        delay = base_delay * (1 + self._jitter * random_value)
        await self._sleep(min(delay, self._max_backoff))

    @staticmethod
    def _http_error(
        status_code: int,
        *,
        attempts: int,
        retryable: bool,
    ) -> APIRequestRejectedError | APIRetryableResponseError:
        error_type: type[APIRequestRejectedError | APIRetryableResponseError]
        if status_code in {401, 403}:
            error_type = APIAuthenticationError
        elif status_code == 404:
            error_type = APINotFoundError
        elif status_code == 409:
            error_type = APIConflictError
        elif status_code == 429:
            error_type = APIRateLimitError
        elif status_code >= 500:
            error_type = APIServerError
        elif status_code in _RETRYABLE_STATUS_CODES:
            error_type = APIRetryableResponseError
        else:
            error_type = APIHTTPError
        return error_type(
            f"VPN Manager returned HTTP {status_code} after {attempts} attempt(s)",
            status_code=status_code,
            attempts=attempts,
            retryable=retryable,
        )

    @staticmethod
    def _idempotency_headers(operation_id: str | None) -> dict[str, str] | None:
        if operation_id is None:
            return None
        if (
            not isinstance(operation_id, str)
            or not operation_id.strip()
            or len(operation_id) > 128
            or "\r" in operation_id
            or "\n" in operation_id
        ):
            raise APIConfigurationError(
                "Operation ID must be a non-empty string of at most 128 characters"
            )
        return {
            _IDEMPOTENCY_HEADER: operation_id,
            # The deployed Manager uses this header for request correlation.
            "X-Request-ID": operation_id,
        }

    @staticmethod
    def _client_path(name: str) -> str:
        return quote(name, safe="")

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, object]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise APIProtocolError("VPN Manager returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise APIProtocolError("VPN Manager returned a non-object JSON payload")
        return cast(dict[str, object], payload)

    @staticmethod
    def _etag_headers(etag: str | None) -> dict[str, str] | None:
        if etag is None:
            return None
        if (
            not isinstance(etag, str)
            or not etag.strip()
            or len(etag) > 512
            or "\r" in etag
            or "\n" in etag
        ):
            raise APIConfigurationError("Inventory ETag is invalid")
        return {"If-None-Match": etag}

    @staticmethod
    def _client_state(payload: object) -> ManagerClientState:
        if not isinstance(payload, dict):
            raise APIProtocolError("VPN Manager client state must be an object")

        name = payload.get("name")
        state = payload.get("state")
        certificate_status = payload.get("certificate_status")
        index_statuses = payload.get("index_statuses")
        issues = payload.get("issues")
        if not isinstance(name, str) or not name:
            raise APIProtocolError("VPN Manager client state has an invalid name")
        if not isinstance(state, str) or state not in _MANAGER_STATES:
            raise APIProtocolError("VPN Manager client state has an invalid state")
        if (
            not isinstance(certificate_status, str)
            or certificate_status not in _CERTIFICATE_STATUSES
        ):
            raise APIProtocolError(
                "VPN Manager client state has an invalid certificate_status"
            )
        if not isinstance(index_statuses, list) or not all(
            isinstance(value, str) for value in index_statuses
        ):
            raise APIProtocolError(
                "VPN Manager client state has invalid index_statuses"
            )
        if not isinstance(issues, list) or not all(
            isinstance(value, str) for value in issues
        ):
            raise APIProtocolError("VPN Manager client state has invalid issues")

        boolean_fields = (
            "suspended",
            "config_present",
            "config_complete",
            "certificate_present",
            "private_key_present",
            "manageable",
        )
        for field in boolean_fields:
            if type(payload.get(field)) is not bool:
                raise APIProtocolError(
                    f"VPN Manager client state has an invalid {field}"
                )

        return ManagerClientState(
            name=name,
            state=cast(ManagerObservedState, state),
            certificate_status=cast(ManagerCertificateStatus, certificate_status),
            index_statuses=tuple(index_statuses),
            suspended=cast(bool, payload["suspended"]),
            config_present=cast(bool, payload["config_present"]),
            config_complete=cast(bool, payload["config_complete"]),
            certificate_present=cast(bool, payload["certificate_present"]),
            private_key_present=cast(bool, payload["private_key_present"]),
            manageable=cast(bool, payload["manageable"]),
            issues=tuple(issues),
        )

    async def create_client(
        self,
        name: str,
        use_password: bool = False,
        *,
        operation_id: str | None = None,
    ) -> str:
        """Ask server to create a new client and return path to .ovpn."""
        r = await self._request(
            "POST",
            "/clients",
            json={"name": name, "use_password": use_password},
            headers=self._idempotency_headers(operation_id),
        )
        config_path = self._json_object(r).get("config_path")
        if not isinstance(config_path, str) or not config_path:
            raise APIProtocolError(
                "VPN Manager response is missing a valid config_path"
            )
        return config_path

    async def download_config(self, name: str) -> bytes:
        name = self._client_path(name)
        r = await self._request("GET", f"/clients/{name}/config")
        return r.content

    async def get_client_inventory(
        self,
        *,
        etag: str | None = None,
    ) -> ManagerClientInventory | None:
        """Return a typed snapshot, or None when Manager responds with 304."""

        response = await self._request(
            "GET",
            "/clients",
            headers=self._etag_headers(etag),
            accepted_status_codes=(304,),
        )
        if response.status_code == 304:
            return None
        payload = self._json_object(response)
        revision = payload.get("revision")
        count = payload.get("count")
        raw_clients = payload.get("clients")
        if not isinstance(revision, str) or not revision:
            raise APIProtocolError("VPN Manager inventory has an invalid revision")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise APIProtocolError("VPN Manager inventory has an invalid count")
        if not isinstance(raw_clients, list):
            raise APIProtocolError("VPN Manager inventory has invalid clients")
        clients = tuple(self._client_state(item) for item in raw_clients)
        if count != len(clients):
            raise APIProtocolError("VPN Manager inventory count does not match clients")
        if len({client.name for client in clients}) != len(clients):
            raise APIProtocolError("VPN Manager inventory has duplicate client names")
        response_etag = response.headers.get("ETag")
        return ManagerClientInventory(
            revision=revision,
            count=count,
            clients=clients,
            etag=response_etag,
        )

    async def get_client_state(self, name: str) -> ManagerClientState:
        """Return one typed Manager client-state record."""

        encoded_name = self._client_path(name)
        response = await self._request("GET", f"/clients/{encoded_name}/state")
        return self._client_state(self._json_object(response))

    async def revoke_client(
        self, name: str, *, operation_id: str | None = None
    ) -> None:
        name = self._client_path(name)
        await self._request(
            "DELETE",
            f"/clients/{name}",
            headers=self._idempotency_headers(operation_id),
        )

    async def suspend_client(
        self, name: str, *, operation_id: str | None = None
    ) -> None:
        name = self._client_path(name)
        await self._request(
            "POST",
            f"/clients/{name}/suspend",
            headers=self._idempotency_headers(operation_id),
        )

    async def unsuspend_client(
        self, name: str, *, operation_id: str | None = None
    ) -> None:
        name = self._client_path(name)
        await self._request(
            "POST",
            f"/clients/{name}/unsuspend",
            headers=self._idempotency_headers(operation_id),
        )

    async def list_blocked(self) -> Sequence[str]:
        r = await self._request("GET", "/clients/blocked")
        blocked_clients = self._json_object(r).get("blocked_clients", [])
        if not isinstance(blocked_clients, list) or not all(
            isinstance(name, str) for name in blocked_clients
        ):
            raise APIProtocolError(
                "VPN Manager response has an invalid blocked_clients value"
            )
        return cast(Sequence[str], blocked_clients)
