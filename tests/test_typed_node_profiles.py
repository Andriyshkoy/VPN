"""Reject Hiddify profile downgrades at the private node API boundary."""

import base64
import copy
import json

import httpx
import pytest

from core.exceptions import APIProtocolError
from core.services.node_gateway import NodeAgentGateway
from core.services.subscription_types import SubscriptionProfile

ACCESS = "48c2163c-f7e2-43e8-87e2-3b75866d28ad"
TOKEN = "typed-profile-test-" + "x" * 32
PASSWORD = "typed-profile-secret-do-not-log"
NATIVE_URI = (
    "hysteria2://synthetic-user:typed-profile-secret-do-not-log@node.example.test:1935/"
    "?sni=node.example.test&insecure=1&pinSHA256=" + "ab" * 32
)


def config():
    return {
        "outbounds": [
            {
                "type": "hysteria2",
                "tag": "Hysteria",
                "server": "node.example.test",
                "server_port": 1935,
                "password": "synthetic-user:" + PASSWORD,
                "obfs": {"type": "salamander", "password": "synthetic-obfs"},
                "tls": {
                    "enabled": True,
                    "server_name": "node.example.test",
                    "insecure": False,
                    "certificate_sha256": [base64.b64encode(bytes(range(32))).decode()],
                },
            }
        ]
    }


def payload(value=None, **overrides):
    return {
        "access_id": ACCESS,
        "service_id": "hysteria",
        "format": "singbox-json",
        "uri": None,
        "content": json.dumps(value if value is not None else config()),
        "contains_secret": True,
        **overrides,
    }


def gateway(value, seen=None):
    def handle(request):
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, json=value)

    return NodeAgentGateway(
        "127.0.0.1",
        18010,
        TOKEN,
        node_id="nl-1",
        transport=httpx.MockTransport(handle),
        backoff=0,
    )


async def test_hiddify_json_profile_preserves_pin_and_redacts_both_dtos():
    response = payload()
    async with gateway(response) as api:
        profile = await api.export_profile(ACCESS)
    assert profile.format == "singbox-json" and profile.uri is None
    assert json.loads(profile.content) == config()
    assert PASSWORD not in repr(profile)
    product = SubscriptionProfile(
        service_id="hysteria",
        uri=None,
        format=profile.format,
        content=profile.content,
    )
    assert PASSWORD not in repr(product)


@pytest.mark.parametrize("explicit", [False, True])
async def test_hysteria_native_uri_requires_explicit_native_client_request(explicit):
    response = payload(format="uri", uri=NATIVE_URI, content=None)
    seen = []
    async with gateway(response, seen) as api:
        if explicit:
            profile = await api.export_profile(ACCESS, profile_format="native-uri")
            assert profile.uri == NATIVE_URI and profile.content is None
            assert seen[0].url.params["format"] == "native-uri"
        else:
            with pytest.raises(APIProtocolError) as error:
                await api.export_profile(ACCESS)
            assert PASSWORD not in str(error.value)


@pytest.mark.parametrize(
    "mutation",
    [
        "tls_missing",
        "pin_missing",
        "pin_hex",
        "pin_wrong_size",
        "pin_invalid_base64",
        "pin_not_array",
        "insecure",
        "tls_disabled",
        "server_port_bool",
        "server_port_zero",
        "missing_sni",
        "empty_password",
        "wrong_protocol",
        "extra_outbound",
        "extra_top_level",
        "extra_tls_key",
        "extra_obfs_key",
    ],
)
async def test_hiddify_profile_rejects_downgrade_or_unexpected_instructions(mutation):
    value = copy.deepcopy(config())
    outbound = value["outbounds"][0]
    tls = outbound["tls"]
    if mutation == "tls_missing":
        del outbound["tls"]
    elif mutation == "pin_missing":
        del tls["certificate_sha256"]
    elif mutation == "pin_hex":
        tls["certificate_sha256"] = ["ab" * 32]
    elif mutation == "pin_wrong_size":
        tls["certificate_sha256"] = [base64.b64encode(b"short").decode()]
    elif mutation == "pin_invalid_base64":
        tls["certificate_sha256"] = ["!" * 44]
    elif mutation == "pin_not_array":
        tls["certificate_sha256"] = tls["certificate_sha256"][0]
    elif mutation == "insecure":
        tls["insecure"] = True
    elif mutation == "tls_disabled":
        tls["enabled"] = False
    elif mutation == "server_port_bool":
        outbound["server_port"] = True
    elif mutation == "server_port_zero":
        outbound["server_port"] = 0
    elif mutation == "missing_sni":
        del tls["server_name"]
    elif mutation == "empty_password":
        outbound["password"] = ""
    elif mutation == "wrong_protocol":
        outbound["type"] = "direct"
    elif mutation == "extra_outbound":
        value["outbounds"].append({"type": "direct", "tag": "unverified"})
    elif mutation == "extra_top_level":
        value["route"] = {"final": "unverified"}
    elif mutation == "extra_tls_key":
        tls["disable_sni"] = True
    elif mutation == "extra_obfs_key":
        outbound["obfs"]["script"] = "unexpected"
    async with gateway(payload(value)) as api:
        with pytest.raises(APIProtocolError) as error:
            await api.export_profile(ACCESS)
    assert PASSWORD not in str(error.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"uri": NATIVE_URI},
        {"service_id": "vless-reality"},
        {"contains_secret": False},
        {"access_id": "8bfbcdf9-dfef-4519-8c6f-c18256072a96"},
        {"content": None},
        {"content": "[]"},
        {"content": "x" * 100_000},
    ],
)
async def test_typed_profile_identity_and_exclusive_secret_fields(overrides):
    async with gateway(payload(**overrides)) as api:
        with pytest.raises(APIProtocolError):
            await api.export_profile(ACCESS)


async def test_duplicate_json_fields_cannot_hide_security_changes():
    encoded = json.dumps(config()).replace(
        '"insecure": false', '"insecure": true, "insecure": false'
    )
    async with gateway(payload(content=encoded)) as api:
        with pytest.raises(APIProtocolError):
            await api.export_profile(ACCESS)
