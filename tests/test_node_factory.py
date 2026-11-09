from __future__ import annotations

import json

import pytest

from core.config import Settings, settings
from core.exceptions import APIConfigurationError
from core.services.node_factory import node_gateway_options


def test_wireguard_factory_does_not_use_unrelated_https_identity(tmp_path, monkeypatch):
    token = "test-only-node-token-" + "x" * 32
    catalog = tmp_path / "endpoints.json"
    catalog.write_text(
        json.dumps(
            {
                "nl-1": {
                    "base_url": "http://10.77.78.2:18010",
                    "token": token,
                    "transport": "wireguard-http",
                }
            }
        )
    )
    catalog.chmod(0o600)
    monkeypatch.setattr(settings, "node_agent_endpoints_file", str(catalog))
    monkeypatch.setattr(settings, "node_agent_ca_cert_path", "/unrelated/https-ca.crt")
    monkeypatch.setattr(
        settings, "node_agent_client_cert_path", "/unrelated/client.crt"
    )
    monkeypatch.setattr(settings, "node_agent_client_key_path", "/unrelated/client.key")
    assert node_gateway_options("nl-1", "10.77.78.2", 18010, token) == {
        "trusted_wireguard_base_url": "http://10.77.78.2:18010",
        "tls_enabled": False,
    }
    with pytest.raises(APIConfigurationError):
        node_gateway_options("nl-1", "10.77.78.3", 18010, token)


def test_default_node_factory_keeps_separate_verified_https_material(monkeypatch):
    monkeypatch.setattr(settings, "node_agent_endpoints_file", "")
    monkeypatch.setattr(settings, "node_agent_ca_cert_path", "/node-ca.crt")
    monkeypatch.setattr(settings, "node_agent_client_cert_path", "/node-client.crt")
    monkeypatch.setattr(settings, "node_agent_client_key_path", "/node-client.key")
    assert node_gateway_options("nl-1", "node.example.invalid", 443, "x" * 32) == {
        "ca_cert_path": "/node-ca.crt",
        "client_cert_path": "/node-client.crt",
        "client_key_path": "/node-client.key",
    }


def test_node_tls_identity_must_be_a_pair(monkeypatch):
    monkeypatch.setenv("NODE_AGENT_CLIENT_CERT_PATH", "/cert-only.crt")
    monkeypatch.setenv("NODE_AGENT_CLIENT_KEY_PATH", "")
    with pytest.raises(ValueError, match="certificate and key"):
        Settings()
