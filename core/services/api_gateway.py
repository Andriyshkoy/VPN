from typing import Sequence

import httpx


class APIGateway:
    """Thin async wrapper around the remote VPN‑management REST API."""

    def __init__(self, ip: str | object, port: int | None = None, api_key: str | None = None, *, timeout: float = 20.0) -> None:
        if port is None and hasattr(ip, "ip"):
            server = ip
            ip = server.ip
            port = server.port
            api_key = server.api_key
        assert port is not None and api_key is not None
        self._base_url = f"http://{ip}:{port}"
        self._headers = {"X-API-Key": api_key}
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "APIGateway":
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=self._timeout,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._client:
            await self._client.aclose()

    async def create_client(self, name: str, use_password: bool = False) -> str:
        """Ask server to create a new client and return path to .ovpn."""
        r = await self._client.post(
            "/clients",
            json={"name": name, "use_password": use_password},
        )
        r.raise_for_status()
        return r.json()["config_path"]

    async def download_config(self, name: str) -> bytes:
        r = await self._client.get(f"/clients/{name}/config")
        r.raise_for_status()
        return r.content

    async def revoke_client(self, name: str) -> None:
        r = await self._client.delete(f"/clients/{name}")
        r.raise_for_status()

    async def suspend_client(self, name: str) -> None:
        r = await self._client.post(f"/clients/{name}/suspend")
        r.raise_for_status()

    async def unsuspend_client(self, name: str) -> None:
        r = await self._client.post(f"/clients/{name}/unsuspend")
        r.raise_for_status()

    async def list_blocked(self) -> Sequence[str]:
        r = await self._client.get("/clients/blocked")
        r.raise_for_status()
        return r.json().get("blocked_clients", [])
