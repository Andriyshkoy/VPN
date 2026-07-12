from collections import deque

import httpx
import pytest

from core.exceptions import (
    APIAuthenticationError,
    APIConfigurationError,
    APIConflictError,
    APIConnectionError,
    APIHTTPError,
    APINotFoundError,
    APIProtocolError,
    APIRequestRejectedError,
    APIServerError,
    APITransportError,
)
from core.services.api_gateway import APIGateway


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes = deque(outcomes)
        self.requests = []
        self.closed = False

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        outcome = self.outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def aclose(self):
        self.closed = True


def response(status_code, json=None, *, content=None):
    request = httpx.Request("GET", "http://vpn.example.test/resource")
    if json is not None:
        return httpx.Response(status_code, json=json, request=request)
    return httpx.Response(status_code, content=content or b"", request=request)


async def configured_gateway(outcomes, **kwargs):
    gateway = APIGateway("vpn.example.test", 8080, "secret", **kwargs)
    client = FakeClient(outcomes)
    gateway._client = client
    return gateway, client


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("", 8080, "secret"), "host"),
        (("vpn.example.test", None, "secret"), "port"),
        (("vpn.example.test", 0, "secret"), "port"),
        (("vpn.example.test", 8080, ""), "API key"),
    ],
)
def test_constructor_rejects_invalid_configuration(args, message):
    with pytest.raises(APIConfigurationError, match=message):
        APIGateway(*args)


@pytest.mark.asyncio
async def test_get_retries_transport_and_retryable_status_exponentially():
    request = httpx.Request("GET", "http://vpn.example.test/clients/blocked")
    sleeps = []

    async def record_sleep(delay):
        sleeps.append(delay)

    gateway, client = await configured_gateway(
        [
            httpx.ConnectTimeout("timed out", request=request),
            response(503),
            response(200, {"blocked_clients": ["alice"]}),
        ],
        retries=2,
        backoff=0.5,
        jitter=0,
        sleep=record_sleep,
    )

    assert await gateway.list_blocked() == ["alice"]
    assert len(client.requests) == 3
    assert sleeps == [0.5, 1.0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (400, APIHTTPError),
        (401, APIAuthenticationError),
        (404, APINotFoundError),
        (409, APIConflictError),
    ],
)
async def test_non_retryable_http_status_is_classified_and_not_retried(
    status_code, error_type
):
    gateway, client = await configured_gateway(
        [response(status_code), response(200, {"blocked_clients": []})],
        retries=3,
    )

    with pytest.raises(error_type) as caught:
        await gateway.list_blocked()

    assert caught.value.status_code == status_code
    assert caught.value.attempts == 1
    assert caught.value.retryable is False
    assert isinstance(caught.value, APIRequestRejectedError)
    assert not isinstance(caught.value, APIConnectionError)
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_retryable_status_exhaustion_is_classified():
    gateway, client = await configured_gateway(
        [response(503), response(503)], retries=1, backoff=0, jitter=0
    )

    with pytest.raises(APIServerError) as caught:
        await gateway.list_blocked()

    assert caught.value.status_code == 503
    assert caught.value.attempts == 2
    assert caught.value.retryable is True
    assert isinstance(caught.value, APIConnectionError)
    assert len(client.requests) == 2


@pytest.mark.asyncio
async def test_mutating_post_is_not_retried_without_operation_id():
    request = httpx.Request("POST", "http://vpn.example.test/clients")
    gateway, client = await configured_gateway(
        [
            httpx.ReadTimeout("uncertain outcome", request=request),
            response(200, {"config_path": "/tmp/client.ovpn"}),
        ],
        retries=3,
    )

    with pytest.raises(APITransportError) as caught:
        await gateway.create_client("client")

    assert caught.value.attempts == 1
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_operation_id_enables_safe_mutation_retry_and_is_forwarded():
    request = httpx.Request("POST", "http://vpn.example.test/clients")
    gateway, client = await configured_gateway(
        [
            httpx.ConnectTimeout("timed out", request=request),
            response(200, {"config_path": "/tmp/client.ovpn"}),
        ],
        retries=1,
        backoff=0,
        jitter=0,
    )

    path = await gateway.create_client("client", operation_id="provision-42")

    assert path == "/tmp/client.ovpn"
    assert len(client.requests) == 2
    assert all(
        call[2]["headers"]
        == {
            "Idempotency-Key": "provision-42",
            "X-Request-ID": "provision-42",
        }
        for call in client.requests
    )


@pytest.mark.asyncio
async def test_client_name_is_escaped_in_path():
    gateway, client = await configured_gateway([response(200, content=b"config")])

    assert await gateway.download_config("name/with space") == b"config"
    assert client.requests[0][1] == "/clients/name%2Fwith%20space/config"


@pytest.mark.asyncio
async def test_malformed_success_payload_is_protocol_error():
    gateway, _ = await configured_gateway([response(200, {"unexpected": True})])

    with pytest.raises(APIProtocolError, match="config_path"):
        await gateway.create_client("client", operation_id="provision-1")
