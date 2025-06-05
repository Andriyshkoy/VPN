from __future__ import annotations

from typing import Sequence
import asyncio

import httpx


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
    ) -> None:
        """
        Initialize the API gateway with server details.
        Args:
            ip: IP address or object with `ip` and `port` attributes
            port: Port number of the API (if not provided, will be taken from `ip`)
            api_key: API key for authentication
            timeout: Request timeout in seconds
            retries: Number of retry attempts on failure
            backoff: Backoff time in seconds between retries
        """
        if port is None and hasattr(ip, "ip"):
            server = ip
            ip = server.ip
            port = server.port
            api_key = server.api_key
        assert port is not None and api_key is not None
        self._base_url = f"http://{ip}:{port}"
        self._headers = {"X-API-Key": api_key}
        self._timeout = timeout
        self._retries = retries
        self._backoff = backoff
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "APIGateway":
        await self._create_client()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._client:
            await self._client.aclose()

    async def _create_client(self) -> None:
        if self._client:
            await self._client.aclose()
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=self._timeout,
        )

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        attempts = 0
        while True:
            try:
                assert self._client is not None
                response = await self._client.request(method, url, **kwargs)
                response.raise_for_status()
                return response
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                attempts += 1
                if attempts > self._retries:
                    raise
                await self._create_client()
                await asyncio.sleep(self._backoff * attempts)

    async def create_client(self, name: str, use_password: bool = False) -> str:
        """Ask server to create a new client and return path to .ovpn."""
        r = await self._request(
            "POST",
            "/clients",
            json={"name": name, "use_password": use_password},
        )
        return r.json()["config_path"]

    async def download_config(self, name: str) -> bytes:
        r = await self._request("GET", f"/clients/{name}/config")
        return r.content

    async def revoke_client(self, name: str) -> None:
        await self._request("DELETE", f"/clients/{name}")

    async def suspend_client(self, name: str) -> None:
        await self._request("POST", f"/clients/{name}/suspend")

    async def unsuspend_client(self, name: str) -> None:
        await self._request("POST", f"/clients/{name}/unsuspend")

    async def list_blocked(self) -> Sequence[str]:
        r = await self._client.get("/clients/blocked")
        r = await self._request("GET", "/clients/blocked")
        return r.json().get("blocked_clients", [])
