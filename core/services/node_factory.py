"""One trust-policy boundary shared by provisioning and fleet observations."""

from __future__ import annotations

from core.config import settings

from .node_endpoints import resolve_node_endpoint


def node_gateway_options(node_id: str, ip: str, port: int, api_key: str) -> dict:
    options = resolve_node_endpoint(
        node_id,
        ip,
        port,
        api_key,
        settings.node_agent_endpoints_file,
    )
    if options.get("tls_enabled") is False:
        # The private catalog explicitly selects the encrypted WireGuard path.
        # Unrelated HTTPS identities must not turn this into an unverified TLS path.
        return options
    return {
        **options,
        "ca_cert_path": settings.node_agent_ca_cert_path or None,
        "client_cert_path": settings.node_agent_client_cert_path or None,
        "client_key_path": settings.node_agent_client_key_path or None,
    }
