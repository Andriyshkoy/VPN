from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from ipaddress import IPv6Address
from types import TracebackType
from typing import Any, cast
from urllib.parse import quote

import httpx

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
    APITransportError,
)

_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_IDEMPOTENCY_HEADER = "Idempotency-Key"

Sleep = Callable[[float], Awaitable[None]]
Random = Callable[[], float]


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

        self._base_url = f"http://{host}:{validated_port}"
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
        )

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        client = self._client
        if client is None:
            raise APIConfigurationError(
                "APIGateway must be entered as an async context manager"
            )

        method = method.upper()
        can_retry = self._can_retry(method, kwargs.get("headers"))
        max_attempts = self._retries + 1 if can_retry else 1

        for attempt in range(1, max_attempts + 1):
            try:
                response = await client.request(method, url, **kwargs)
            except httpx.RequestError as exc:
                if attempt >= max_attempts:
                    raise APITransportError(
                        f"VPN Manager transport failure after {attempt} attempt(s)",
                        attempts=attempt,
                    ) from exc
                await self._wait_before_retry(attempt)
                continue

            if 200 <= response.status_code < 300:
                return response

            status_is_retryable = response.status_code in _RETRYABLE_STATUS_CODES
            if status_is_retryable and can_retry and attempt < max_attempts:
                await self._wait_before_retry(attempt)
                continue

            raise self._http_error(
                response.status_code,
                attempts=attempt,
                retryable=status_is_retryable and can_retry,
            )

        raise RuntimeError("unreachable")  # pragma: no cover

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
