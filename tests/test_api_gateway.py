from collections import deque

import httpx
import pytest
from pydantic import ValidationError

from core.config import Settings, settings
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
    APITLSConfigurationError,
    APITransportError,
)
from core.services.api_gateway import (
    APIGateway,
    ManagerClientInventory,
    ManagerClientState,
)


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


def response(status_code, json=None, *, content=None, headers=None):
    request = httpx.Request("GET", "http://vpn.example.test/resource")
    if json is not None:
        return httpx.Response(
            status_code,
            json=json,
            headers=headers,
            request=request,
        )
    return httpx.Response(
        status_code,
        content=content or b"",
        headers=headers,
        request=request,
    )


def client_state_payload(name="client", state="active"):
    return {
        "name": name,
        "state": state,
        "certificate_status": "valid",
        "index_statuses": ["V"],
        "suspended": state == "suspended",
        "config_present": True,
        "config_complete": True,
        "certificate_present": True,
        "private_key_present": True,
        "manageable": True,
        "issues": [],
    }


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


def test_legacy_http_is_default_and_tls_can_be_enabled(monkeypatch):
    class DummyContext:
        def __init__(self):
            self.loaded = None

        def load_cert_chain(self, *, certfile, keyfile):
            self.loaded = (certfile, keyfile)

    context = DummyContext()
    calls = []

    def create_context(*, cafile=None):
        calls.append(cafile)
        return context

    monkeypatch.setattr(
        "core.services.api_gateway.ssl.create_default_context", create_context
    )

    legacy = APIGateway("vpn.example.test", 8080, "secret", tls_enabled=False)
    secure = APIGateway(
        "vpn.example.test",
        8080,
        "secret",
        tls_enabled=True,
        mtls_required=True,
        tls_port=8443,
        ca_cert_path="/run/secrets/vpn-manager/ca.crt",
        client_cert_path="/run/secrets/vpn-manager/client.crt",
        client_key_path="/run/secrets/vpn-manager/client.key",
    )

    assert legacy._base_url == "http://vpn.example.test:8080"
    assert legacy._tls_context is None
    assert secure._base_url == "https://vpn.example.test:8443"
    assert secure._tls_context is context
    assert calls == ["/run/secrets/vpn-manager/ca.crt"]
    assert context.loaded == (
        "/run/secrets/vpn-manager/client.crt",
        "/run/secrets/vpn-manager/client.key",
    )


def test_tls_rejects_partial_client_identity(monkeypatch):
    monkeypatch.setattr(
        "core.services.api_gateway.ssl.create_default_context",
        lambda **kwargs: object(),
    )

    with pytest.raises(APITLSConfigurationError, match="configured together"):
        APIGateway(
            "vpn.example.test",
            8443,
            "secret",
            tls_enabled=True,
            client_cert_path="client.crt",
        )


def test_mtls_required_fails_closed_for_missing_or_unreadable_material():
    with pytest.raises(APITLSConfigurationError, match="requires CA"):
        APIGateway(
            "vpn.example.test",
            8443,
            "secret",
            tls_enabled=True,
            mtls_required=True,
            ca_cert_path="ca.crt",
            client_cert_path="client.crt",
            client_key_path="",
        )

    with pytest.raises(APITLSConfigurationError, match="invalid or unreadable"):
        APIGateway(
            "vpn.example.test",
            8443,
            "secret",
            tls_enabled=True,
            ca_cert_path="/definitely/missing/manager-ca.crt",
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "vpn_manager_tls_enabled": False,
            "vpn_manager_mtls_required": True,
            "vpn_manager_ca_cert_path": "ca.crt",
            "vpn_manager_client_cert_path": "client.crt",
            "vpn_manager_client_key_path": "client.key",
        },
        {
            "vpn_manager_tls_enabled": True,
            "vpn_manager_mtls_required": True,
            "vpn_manager_ca_cert_path": "ca.crt",
            "vpn_manager_client_cert_path": "client.crt",
            "vpn_manager_client_key_path": "",
        },
        {
            "vpn_manager_tls_enabled": True,
            "vpn_manager_mtls_required": False,
            "vpn_manager_ca_cert_path": "",
            "vpn_manager_client_cert_path": "client.crt",
            "vpn_manager_client_key_path": "",
        },
    ],
)
def test_settings_reject_inconsistent_tls_configuration(overrides):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            database_url=settings.database_url,
            encryption_key=settings.encryption_key,
            **overrides,
        )


def test_settings_accept_complete_mtls_paths_without_reading_mounts():
    configured = Settings(
        _env_file=None,
        database_url=settings.database_url,
        encryption_key=settings.encryption_key,
        vpn_manager_tls_enabled=True,
        vpn_manager_mtls_required=True,
        vpn_manager_ca_cert_path="/not-mounted-yet/ca.crt",
        vpn_manager_client_cert_path="/not-mounted-yet/client.crt",
        vpn_manager_client_key_path="/not-mounted-yet/client.key",
    )

    assert configured.vpn_manager_mtls_required is True


def test_tls_settings_switch_scheme_and_override_legacy_port(monkeypatch):
    context = object()
    monkeypatch.setattr(settings, "vpn_manager_tls_enabled", True)
    monkeypatch.setattr(settings, "vpn_manager_mtls_required", False)
    monkeypatch.setattr(settings, "vpn_manager_tls_port", 16291)
    monkeypatch.setattr(settings, "vpn_manager_ca_cert_path", "")
    monkeypatch.setattr(settings, "vpn_manager_client_cert_path", "")
    monkeypatch.setattr(settings, "vpn_manager_client_key_path", "")
    monkeypatch.setattr(
        "core.services.api_gateway.ssl.create_default_context",
        lambda **kwargs: context,
    )

    gateway = APIGateway("10.77.77.2", 16290, "secret")

    assert gateway._base_url == "https://10.77.77.2:16291"
    assert gateway._tls_context is context


@pytest.mark.asyncio
async def test_httpx_client_ignores_proxy_environment(monkeypatch):
    captured = {}

    class DummyAsyncClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def aclose(self):
            return None

    monkeypatch.setattr("core.services.api_gateway.httpx.AsyncClient", DummyAsyncClient)
    gateway = APIGateway(
        "vpn.example.test",
        8080,
        "secret",
        tls_enabled=False,
    )

    await gateway._create_client()

    assert captured["trust_env"] is False


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


@pytest.mark.asyncio
async def test_inventory_is_typed_and_forwards_etag():
    payload = client_state_payload("alice", "suspended")
    gateway, client = await configured_gateway(
        [
            response(
                200,
                {
                    "revision": "sha256:inventory",
                    "count": 1,
                    "clients": [payload],
                },
                headers={"ETag": '"sha256:inventory"'},
            )
        ]
    )

    result = await gateway.get_client_inventory(etag='"sha256:previous"')

    assert isinstance(result, ManagerClientInventory)
    assert result.etag == '"sha256:inventory"'
    assert isinstance(result.clients[0], ManagerClientState)
    assert result.clients[0].name == "alice"
    assert result.clients[0].state == "suspended"
    assert client.requests[0][1] == "/clients"
    assert client.requests[0][2]["headers"] == {"If-None-Match": '"sha256:previous"'}


@pytest.mark.asyncio
async def test_inventory_returns_none_for_not_modified():
    gateway, client = await configured_gateway([response(304)])

    assert await gateway.get_client_inventory(etag='"sha256:same"') is None
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_inventory_rejects_malformed_count_and_state():
    gateway, _ = await configured_gateway(
        [
            response(
                200,
                {
                    "revision": "sha256:bad",
                    "count": 2,
                    "clients": [client_state_payload()],
                },
            )
        ]
    )
    with pytest.raises(APIProtocolError, match="count does not match"):
        await gateway.get_client_inventory()

    malformed = client_state_payload()
    malformed["manageable"] = "yes"
    gateway, _ = await configured_gateway([response(200, malformed)])
    with pytest.raises(APIProtocolError, match="manageable"):
        await gateway.get_client_state("client")


@pytest.mark.asyncio
async def test_inventory_rejects_duplicate_names():
    payload = client_state_payload("duplicate")
    gateway, _ = await configured_gateway(
        [
            response(
                200,
                {
                    "revision": "sha256:duplicate",
                    "count": 2,
                    "clients": [payload, payload],
                },
            )
        ]
    )

    with pytest.raises(APIProtocolError, match="duplicate"):
        await gateway.get_client_inventory()


@pytest.mark.asyncio
async def test_get_client_state_is_typed_and_escapes_name():
    gateway, client = await configured_gateway(
        [response(200, client_state_payload("name/with space"))]
    )

    state = await gateway.get_client_state("name/with space")

    assert isinstance(state, ManagerClientState)
    assert state.name == "name/with space"
    assert client.requests[0][1] == "/clients/name%2Fwith%20space/state"
