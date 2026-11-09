import json
import os

import pytest

from core.exceptions import APIConfigurationError
from core.services.node_endpoints import load_node_endpoints, resolve_node_endpoint
from core.services.node_gateway import NodeAgentGateway

TOKEN = "test-only-node-token-" + "x" * 32
ORIGIN = "http://10.77.78.2:18010"


def write_catalog(tmp_path, value=None, mode=0o600):
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(
            value
            if value is not None
            else {
                "nl-1": {
                    "base_url": ORIGIN,
                    "token": TOKEN,
                    "transport": "wireguard-http",
                }
            }
        )
    )
    path.chmod(mode)
    return path


def test_exact_private_registration_enables_wireguard_origin(tmp_path):
    path = write_catalog(tmp_path)
    kwargs = resolve_node_endpoint("nl-1", "10.77.78.2", 18010, TOKEN, path)
    assert kwargs == {"trusted_wireguard_base_url": ORIGIN, "tls_enabled": False}
    api = NodeAgentGateway("10.77.78.2", 18010, TOKEN, node_id="nl-1", **kwargs)
    assert api._base_url == ORIGIN
    assert TOKEN not in repr(api)


@pytest.mark.parametrize(
    "field,value",
    [
        ("node_id", "another-node"),
        ("ip", "10.77.78.3"),
        ("port", 18011),
        ("api_key", "other-secret-" + "y" * 32),
        ("ip", "10.77.78.2.evil.test"),
    ],
)
def test_any_database_registration_mismatch_is_rejected_before_network(
    tmp_path, field, value
):
    path = write_catalog(tmp_path)
    fields = {
        "node_id": "nl-1",
        "ip": "10.77.78.2",
        "port": 18010,
        "api_key": TOKEN,
        "catalogpath": path,
        field: value,
    }
    with pytest.raises(APIConfigurationError) as error:
        resolve_node_endpoint(**fields)
    assert TOKEN not in str(error.value) and ORIGIN not in str(error.value)


@pytest.mark.parametrize("path", [None, ""])
def test_absent_catalog_preserves_https_default(path):
    assert resolve_node_endpoint("nl-1", "10.77.78.2", 18010, TOKEN, path) == {}
    api = NodeAgentGateway("10.77.78.2", 18010, TOKEN, node_id="nl-1")
    assert api._base_url == "https://10.77.78.2:18010"
    with pytest.raises(APIConfigurationError):
        NodeAgentGateway("10.77.78.2", 18010, TOKEN, node_id="nl-1", tls_enabled=False)


@pytest.mark.parametrize(
    "origin",
    [
        "http://vpn.example.test:18010",
        "http://10.77.78.2:18010/",
        "http://user:password@10.77.78.2:18010",
        "http://10.77.78.2:18010?token=secret",
        "http://10.77.78.2:18010#secret",
        "http://10.77.78.2:18010?",
        "https://10.77.78.2:18010",
        "http://10.77.78.2",
        "http://010.77.78.2:18010",
        "http://10.77.78.2:018010",
        "http://[fe80::1%eth0]:18010",
        "http://0.0.0.0:18010",
        "http://224.0.0.1:18010",
    ],
)
def test_catalog_rejects_dns_credentials_paths_queries_and_noncanonical_origins(
    tmp_path, origin
):
    path = write_catalog(
        tmp_path,
        {"nl-1": {"base_url": origin, "token": TOKEN, "transport": "wireguard-http"}},
    )
    with pytest.raises(APIConfigurationError) as error:
        load_node_endpoints(path)
    assert TOKEN not in str(error.value) and origin not in str(error.value)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o440, 0o660, 0o700, 0o666])
def test_catalog_requires_private_owner_permissions(tmp_path, mode):
    with pytest.raises(APIConfigurationError):
        load_node_endpoints(write_catalog(tmp_path, mode=mode))


@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_catalog_accepts_owner_readonly_and_private_readwrite(tmp_path, mode):
    assert (
        load_node_endpoints(write_catalog(tmp_path, mode=mode))["nl-1"]["base_url"]
        == ORIGIN
    )


def test_symlink_directory_and_fifo_are_not_accepted(tmp_path):
    path = write_catalog(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo, 0o600)
    for invalid in (link, tmp_path, fifo):
        with pytest.raises(APIConfigurationError):
            load_node_endpoints(invalid)


def test_foreign_file_owner_is_rejected(tmp_path, monkeypatch):
    path = write_catalog(tmp_path)
    real_stat = os.fstat

    def wrong_owner(descriptor):
        info = real_stat(descriptor)
        fields = list(info)
        fields[4] = 12345
        return os.stat_result(fields)

    monkeypatch.setattr("core.services.node_endpoints.os.fstat", wrong_owner)
    with pytest.raises(APIConfigurationError):
        load_node_endpoints(path)


@pytest.mark.parametrize(
    "value",
    [
        [],
        {},
        {
            "../node": {
                "base_url": ORIGIN,
                "token": TOKEN,
                "transport": "wireguard-http",
            }
        },
        {"nl-1": {"base_url": ORIGIN, "token": TOKEN, "transport": "http"}},
        {
            "nl-1": {
                "base_url": ORIGIN,
                "token": TOKEN,
                "transport": "wireguard-http",
                "extra": "bad",
            }
        },
        {"nl-1": {"base_url": ORIGIN, "token": "short", "transport": "wireguard-http"}},
        {
            "nl-1": {
                "base_url": ORIGIN,
                "token": TOKEN + "\r\n",
                "transport": "wireguard-http",
            }
        },
        {
            "nl-1": {
                "base_url": ORIGIN,
                "token": TOKEN + "\u0000",
                "transport": "wireguard-http",
            }
        },
    ],
)
def test_invalid_catalog_schema_is_rejected(tmp_path, value):
    with pytest.raises(APIConfigurationError):
        load_node_endpoints(write_catalog(tmp_path, value))


def test_unreadable_malformed_oversized_or_duplicate_catalog_never_echoes_data(
    tmp_path,
):
    path = write_catalog(tmp_path)
    for text in ("not-json" + TOKEN, " " * 65537, '{"nl-1":{},"nl-1":{}}'):
        path.write_text(text)
        with pytest.raises(APIConfigurationError) as error:
            load_node_endpoints(path)
        assert TOKEN not in str(error.value) and str(path) not in str(error.value)
    with pytest.raises(APIConfigurationError):
        load_node_endpoints(tmp_path / "does-not-exist")


def test_ipv6_database_address_is_normalized_against_exact_registered_origin(tmp_path):
    origin = "http://[fd77:78::2]:18010"
    path = write_catalog(
        tmp_path,
        {"nl-1": {"base_url": origin, "token": TOKEN, "transport": "wireguard-http"}},
    )
    kwargs = resolve_node_endpoint(
        "nl-1", "fd77:0078:0000:0000:0000:0000:0000:0002", 18010, TOKEN, path
    )
    api = NodeAgentGateway("fd77:78::2", 18010, TOKEN, node_id="nl-1", **kwargs)
    assert api._base_url == origin


def test_explicit_gateway_exception_does_not_allow_other_host_or_port():
    for ip, port in (("10.77.78.3", 18010), ("10.77.78.2", 18011)):
        with pytest.raises(APIConfigurationError):
            NodeAgentGateway(
                ip, port, TOKEN, node_id="nl-1", trusted_wireguard_base_url=ORIGIN
            )
    with pytest.raises(APIConfigurationError):
        NodeAgentGateway(
            "10.77.78.2",
            18010,
            TOKEN,
            node_id="nl-1",
            trusted_wireguard_base_url=ORIGIN,
            tls_enabled=True,
        )
