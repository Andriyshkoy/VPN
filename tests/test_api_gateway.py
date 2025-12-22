import httpx
import pytest

from core.exceptions import APIConnectionError
from core.services.api_gateway import APIGateway


@pytest.mark.asyncio
async def test_api_gateway_retries_and_succeeds(monkeypatch):
    statuses = [500, 502, 200]

    def handler(request):
        status = statuses.pop(0)
        return httpx.Response(status, json={"config_path": "/tmp/cfg"})

    transport = httpx.MockTransport(handler)

    async def create_client(self):
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=self._timeout,
            transport=transport,
        )

    monkeypatch.setattr(APIGateway, "_create_client", create_client)

    async with APIGateway("1.1.1.1", 80, "k", retries=3, backoff=0) as api:
        path = await api.create_client("name")

    assert path == "/tmp/cfg"
    assert statuses == []


@pytest.mark.asyncio
async def test_api_gateway_raises_after_retries(monkeypatch):
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("boom", request=request)

    transport = httpx.MockTransport(handler)

    async def create_client(self):
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._headers,
            timeout=self._timeout,
            transport=transport,
        )

    monkeypatch.setattr(APIGateway, "_create_client", create_client)

    async with APIGateway("1.1.1.1", 80, "k", retries=1, backoff=0) as api:
        with pytest.raises(APIConnectionError):
            await api.download_config("name")

    assert attempts == 2
