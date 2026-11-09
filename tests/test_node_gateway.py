import json
from dataclasses import asdict
from uuid import uuid4

import httpx
import pytest

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
from core.services.node_gateway import NodeAgentGateway

TOKEN = "node-test-token-" + "x" * 32
ACCESS = "48c2163c-f7e2-43e8-87e2-3b75866d28ad"
OPERATION = "bc16d517-aee5-415b-8bec-bcc0be746589"
URI = "vless://48c2163c-f7e2-43e8-87e2-3b75866d28ad@vpn.example.test:9443?security=reality&pbk=test-only"


def access_payload(*, service="vless-reality", state="active"):
    return {
        "access": {
            "id": ACCESS,
            "node_id": "nl-1",
            "owner_ref": "hub:config:123",
            "service_id": service,
            "status": state,
            "desired_status": (
                "revoked"
                if state == "revoked"
                else "suspended" if state == "suspended" else "active"
            ),
            "settings": {
                "traffic_limit_bytes": 0,
                "expires_at": None,
                "activation_days": None,
            },
            "effective": {
                "status": state,
                "used_traffic_bytes": 20,
                "traffic_limit_bytes": 0,
                "expires_at": None,
                "last_seen_at": None,
                "expiry_precision": "day" if service == "hysteria" else "second",
                "accounting": "delayed",
                "activation_days": None,
            },
            "revision": 1,
            "operation_pending": False,
            "control_plane_verified": True,
            "data_plane_verified": False,
        },
        "operation_id": str(uuid4()),
    }


def gateway(handler, **kwargs):
    return NodeAgentGateway(
        "127.0.0.1",
        18010,
        TOKEN,
        node_id="nl-1",
        transport=httpx.MockTransport(handler),
        backoff=0,
        **kwargs,
    )


@pytest.mark.parametrize(
    "host",
    [
        "195.133.195.40",
        "10.1.2.3",
        "vpn.example.test",
        "127.0.0.1.evil.test",
        "localhost.evil.test",
    ],
)
def test_plain_http_only_allows_literal_loopback(host):
    with pytest.raises(APIConfigurationError):
        NodeAgentGateway(host, 18010, TOKEN, node_id="nl-1", tls_enabled=False)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]"])
def test_loopback_tunnel_can_use_plain_http(host):
    api = NodeAgentGateway(host, 18010, TOKEN, node_id="nl-1")
    assert api._base_url.startswith("http://")
    assert TOKEN not in repr(api)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"port": True},
        {"port": 0},
        {"node_id": "../nl"},
        {"api_key": "short"},
        {"ip": "user@host"},
        {"ip": "http://host"},
        {"retries": -1},
        {"timeout": True},
        {"backoff": float("inf")},
    ],
)
def test_invalid_constructor_options_fail_safely(kwargs):
    options = {
        "ip": "127.0.0.1",
        "port": 18010,
        "api_key": TOKEN,
        "node_id": "nl-1",
        **kwargs,
    }
    with pytest.raises(APIConfigurationError) as error:
        NodeAgentGateway(**options)
    assert TOKEN not in str(error.value)


def test_remote_defaults_to_verified_https_and_missing_ca_is_retryable_tls():
    api = NodeAgentGateway("vpn.example.test", 18010, TOKEN, node_id="nl-1")
    assert api._base_url.startswith("https://")
    assert api._tls_context.check_hostname
    with pytest.raises(APITLSConfigurationError):
        NodeAgentGateway(
            "vpn.example.test",
            18010,
            TOKEN,
            node_id="nl-1",
            ca_cert_path="/missing/private-ca.crt",
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"client_cert_path": "cert"},
        {"client_key_path": "key"},
        {"ca_cert_path": "ca", "tls_enabled": False},
    ],
)
def test_tls_configuration_fails_closed(kwargs):
    with pytest.raises(APITLSConfigurationError):
        NodeAgentGateway("127.0.0.1", 18010, TOKEN, node_id="nl-1", **kwargs)


async def test_retry_keeps_exact_bytes_operation_id_and_owned_identity():
    seen = []

    def handle(request):
        seen.append(request)
        if len(seen) == 1:
            raise httpx.ReadTimeout("upstream leaked " + TOKEN, request=request)
        payload = access_payload()
        submitted = json.loads(request.content)
        payload["access"]["settings"] = {
            k: submitted[k]
            for k in ("traffic_limit_bytes", "expires_at", "activation_days")
        }
        return httpx.Response(201, json=payload)

    policy = {"traffic_limit_bytes": 1073741824, "expires_at": "2027-01-01T00:00:00Z"}
    async with gateway(handle) as api:
        result = await api.create_access(
            service_id="vless-reality",
            owner_ref="hub:config:123",
            policy=policy,
            operation_id=OPERATION,
        )
    assert result.id == ACCESS
    assert result.node_id == "nl-1"
    assert len(seen) == 2
    assert seen[0].content == seen[1].content
    assert (
        seen[0].headers["Idempotency-Key"]
        == seen[1].headers["Idempotency-Key"]
        == OPERATION
    )
    assert seen[0].headers["Authorization"] == "Bearer " + TOKEN
    body = json.loads(seen[0].content)
    assert body["node_id"] == "nl-1" and body["owner_ref"] == "hub:config:123"
    assert body["expires_at"] == policy["expires_at"]
    assert "Authorization" not in repr(result)


async def test_retryable_response_keeps_operation_and_body():
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(
            503 if len(seen) == 1 else 200,
            json=(
                {"error": TOKEN}
                if len(seen) == 1
                else access_payload(state="suspended")
            ),
        )

    async with gateway(handle) as api:
        result = await api.suspend_access(ACCESS, operation_id=OPERATION)
    assert result.status == "suspended"
    assert len(seen) == 2
    assert all(r.headers["Idempotency-Key"] == OPERATION for r in seen)
    assert seen[0].content == seen[1].content == b""


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, APIAuthenticationError),
        (403, APIAuthenticationError),
        (404, APINotFoundError),
        (409, APIConflictError),
        (422, APIRequestRejectedError),
        (429, APIServerError),
        (503, APIServerError),
        (302, APIHTTPError),
    ],
)
async def test_http_errors_never_include_remote_bodies_or_credentials(status, expected):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            status,
            json={"secret": TOKEN, "uri": URI},
            headers={"Location": "https://elsewhere.example/"},
        )

    async with gateway(handle, retries=0) as api:
        with pytest.raises(expected) as error:
            await api.get_access(ACCESS)
    assert len(calls) == 1
    assert TOKEN not in str(error.value) and URI not in str(error.value)
    assert TOKEN not in repr(error.value)


async def test_transport_exhaustion_hides_nested_exception_context():
    def handle(request):
        raise httpx.ConnectError("password=" + TOKEN, request=request)

    async with gateway(handle, retries=1) as api:
        with pytest.raises(APITransportError) as error:
            await api.get_access(ACCESS)
    assert error.value.attempts == 2
    assert TOKEN not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize(
    "service,policy",
    [
        ("hysteria", {"expires_at": "2027-01-01T00:00:00Z"}),
        ("vless-reality", {"activation_days": 7}),
        ("hysteria", {"traffic_limit_bytes": True}),
        ("vless-reality", {"expires_at": "2027-01-01T00:00:00"}),
        ("hysteria", {"extra": "secret"}),
    ],
)
async def test_invalid_policy_is_rejected_before_any_network_call(service, policy):
    seen = []
    async with gateway(lambda r: seen.append(r)) as api:
        with pytest.raises(APIConfigurationError):
            await api.create_access(
                service_id=service,
                owner_ref="hub:config:1",
                policy=policy,
                operation_id=OPERATION,
            )
    assert not seen


@pytest.mark.parametrize(
    "path,value",
    [
        ("id", "not-uuid"),
        ("node_id", "other"),
        ("service_id", "openvpn"),
        ("status", "unknown"),
        ("revision", True),
        ("data_plane_verified", True),
        ("operation_pending", 0),
        ("settings", {"uri": URI}),
        ("effective", {"status": "active", "password": TOKEN}),
    ],
)
async def test_malformed_metadata_does_not_enter_domain(path, value):
    payload = access_payload()
    payload["access"][path] = value
    async with gateway(lambda r: httpx.Response(200, json=payload)) as api:
        with pytest.raises(APIProtocolError) as error:
            await api.get_access(ACCESS)
    assert TOKEN not in str(error.value) and URI not in str(error.value)


@pytest.mark.parametrize(
    "state", ["active", "suspended", "revoked", "expired", "limited"]
)
async def test_observed_states_remain_explicit(state):
    async with gateway(
        lambda r: httpx.Response(200, json=access_payload(state=state))
    ) as api:
        result = await api.get_access(ACCESS)
    assert result.status == state
    assert result.effective["status"] == state


async def test_profile_is_explicit_secret_export_and_repr_redacted():
    payload = {
        "access_id": ACCESS,
        "service_id": "vless-reality",
        "format": "uri",
        "uri": URI,
        "contains_secret": True,
    }
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=payload)

    async with gateway(handle) as api:
        profile = await api.export_profile(ACCESS)
    assert profile.uri == URI and URI not in repr(profile)
    assert seen[0].url.path.endswith("/profile")


@pytest.mark.parametrize(
    "override",
    [
        {"contains_secret": False},
        {"format": "ovpn"},
        {"access_id": str(uuid4())},
        {"uri": "https://user:pass@evil.test:443/"},
        {"uri": URI + "\n"},
    ],
)
async def test_invalid_secret_export_is_rejected(override):
    payload = {
        "access_id": ACCESS,
        "service_id": "vless-reality",
        "format": "uri",
        "uri": URI,
        "contains_secret": True,
        **override,
    }
    async with gateway(lambda r: httpx.Response(200, json=payload)) as api:
        with pytest.raises(APIProtocolError) as error:
            await api.export_profile(ACCESS)
    assert URI not in str(error.value)


async def test_readiness_confirms_identity_and_does_not_claim_data_plane():
    def handle(request):
        if request.url.path == "/v1/nodes":
            return httpx.Response(
                200,
                json={
                    "nodes": [{"id": "nl-1", "services": ["hysteria", "vless-reality"]}]
                },
            )
        return httpx.Response(
            503,
            json={
                "ready": False,
                "database_ready": True,
                "services": {
                    "hysteria": {"ready": False},
                    "vless-reality": {"ready": True},
                },
                "scope": "control_plane; no end_to_end_probe",
            },
        )

    async with gateway(handle) as api:
        readiness = await api.ready()
    assert readiness.ready is False
    assert readiness.data_plane_verified is False
    assert readiness.scope == "control_plane"
    assert "certificate" not in json.dumps(asdict(readiness))


async def test_readiness_rejects_wrong_node_before_trusting_health():
    async with gateway(
        lambda r: httpx.Response(
            200,
            json={
                "nodes": [
                    {"id": "another-node", "services": ["hysteria", "vless-reality"]}
                ]
            },
        )
    ) as api:
        with pytest.raises(APIProtocolError):
            await api.ready()


async def test_usage_cannot_claim_authoritative_realtime_billing():
    payload = {
        "access_id": ACCESS,
        "used_traffic_bytes": 123,
        "traffic_limit_bytes": 0,
        "accounting": "delayed",
        "observed_at": "2026-11-09T00:00:00Z",
        "realtime": False,
        "billing_authoritative": False,
    }
    async with gateway(lambda r: httpx.Response(200, json=payload)) as api:
        result = await api.get_usage(ACCESS)
    assert result.used_traffic_bytes == 123
    assert not result.realtime and not result.billing_authoritative
    payload["billing_authoritative"] = True
    async with gateway(lambda r: httpx.Response(200, json=payload)) as api:
        with pytest.raises(APIProtocolError):
            await api.get_usage(ACCESS)


async def test_success_from_wrong_remote_access_is_rejected():
    payload = access_payload()
    payload["access"]["id"] = str(uuid4())
    async with gateway(lambda r: httpx.Response(200, json=payload)) as api:
        with pytest.raises(APIProtocolError):
            await api.resume_access(ACCESS, operation_id=OPERATION)


async def test_hysteria_null_expiry_and_days_are_supported_without_activation_probe():
    seen = []

    def handle(request):
        seen.append(request)
        payload = access_payload(service="hysteria")
        payload["access"]["settings"]["activation_days"] = 7
        payload["access"]["owner_ref"] = "hub:config:1"
        return httpx.Response(201, json=payload)

    async with gateway(handle) as api:
        result = await api.create_access(
            service_id="hysteria",
            owner_ref="hub:config:1",
            policy={"activation_days": 7},
            operation_id=OPERATION,
        )
    assert result.settings["activation_days"] == 7
    assert len(seen) == 1 and seen[0].url.path == "/v1/accesses"


@pytest.mark.parametrize("change", ["owner", "quota", "expiry"])
async def test_created_access_must_match_durable_owner_and_policy(change):
    payload = access_payload()
    if change == "owner":
        payload["access"]["owner_ref"] = "hub:config:another"
    elif change == "quota":
        payload["access"]["settings"]["traffic_limit_bytes"] = 10
    else:
        payload["access"]["settings"]["expires_at"] = "2027-01-01T00:00:00Z"
    async with gateway(lambda r: httpx.Response(201, json=payload)) as api:
        with pytest.raises(APIProtocolError):
            await api.create_access(
                service_id="vless-reality",
                owner_ref="hub:config:123",
                policy={},
                operation_id=OPERATION,
            )


async def test_created_expiry_compares_utc_seconds_not_timestamp_spelling():
    payload = access_payload()
    payload["access"]["settings"]["expires_at"] = "2027-01-01T00:00:00Z"
    async with gateway(lambda r: httpx.Response(201, json=payload)) as api:
        result = await api.create_access(
            service_id="vless-reality",
            owner_ref="hub:config:123",
            policy={"expires_at": "2027-01-01T03:00:00.123456+03:00"},
            operation_id=OPERATION,
        )
    assert result.settings["expires_at"] == "2027-01-01T00:00:00Z"


@pytest.mark.parametrize(
    "field,value", [("service_id", []), ("status", {}), ("desired_status", [])]
)
async def test_unhashable_response_shapes_are_safe_protocol_errors(field, value):
    payload = access_payload()
    payload["access"][field] = value
    async with gateway(lambda r: httpx.Response(200, json=payload)) as api:
        with pytest.raises(APIProtocolError):
            await api.get_access(ACCESS)


async def test_catalog_keeps_typed_public_metadata_and_ignores_unknown_secret_fields():
    services = [
        {
            "id": name,
            "node_id": "nl-1",
            "endpoint": {
                "host": "vpn.example.test",
                "port": 1935 if name == "hysteria" else 9443,
                "transport": "udp" if name == "hysteria" else "tcp",
                "password": TOKEN,
            },
            "capabilities": {
                "fixed_expiry": name != "hysteria",
                "expiry_precision": "day" if name == "hysteria" else "second",
                "unknown_secret": TOKEN,
            },
        }
        for name in ("hysteria", "vless-reality")
    ]
    async with gateway(
        lambda r: httpx.Response(200, json={"services": services})
    ) as api:
        result = await api.catalog()
    assert {s.id for s in result} == {"hysteria", "vless-reality"}
    assert TOKEN not in repr(result)
    assert result[0].endpoint["port"] == 1935


@pytest.mark.parametrize("change", ["duplicate", "wrong_node", "invalid_port"])
async def test_invalid_catalog_is_rejected(change):
    services = [
        {
            "id": name,
            "node_id": "nl-1",
            "endpoint": {"host": "vpn.example.test", "port": 9443, "transport": "tcp"},
            "capabilities": {},
        }
        for name in ("hysteria", "vless-reality")
    ]
    if change == "duplicate":
        services[1]["id"] = "hysteria"
    elif change == "wrong_node":
        services[1]["node_id"] = "other"
    else:
        services[0]["endpoint"]["port"] = True
    async with gateway(
        lambda r: httpx.Response(200, json={"services": services})
    ) as api:
        with pytest.raises(APIProtocolError):
            await api.catalog()


@pytest.mark.parametrize(
    "action,expected_method,suffix,state",
    [
        ("suspend_access", "POST", "/suspend", "suspended"),
        ("resume_access", "POST", "/resume", "active"),
        ("revoke_access", "DELETE", "", "revoked"),
    ],
)
async def test_transitions_use_same_binding_and_durable_operation(
    action, expected_method, suffix, state
):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=access_payload(state=state))

    async with gateway(handle) as api:
        result = await getattr(api, action)(ACCESS, operation_id=OPERATION)
    assert result.id == ACCESS and result.status == state
    assert seen[0].method == expected_method
    assert seen[0].url.path == "/v1/accesses/" + ACCESS + suffix
    assert seen[0].headers["Idempotency-Key"] == OPERATION
