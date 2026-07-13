from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONITORING_SERVICES = (
    "statsd_exporter",
    "postgres_exporter",
    "redis_exporter",
    "prometheus",
)


def test_production_monitoring_is_opt_in_pinned_and_internal():
    compose = (ROOT / "docker-compose-prod.yml").read_text()
    expected_networks = {
        "statsd_exporter": ("app", "monitoring"),
        "postgres_exporter": ("data", "monitoring"),
        "redis_exporter": ("data", "monitoring"),
        "prometheus": ("monitoring",),
    }

    for service_name in MONITORING_SERVICES:
        service = _service_block(compose, service_name)
        assert 'profiles: ["monitoring"]' in service
        assert re.search(r"^    image: .*@sha256:[0-9a-f]{64}$", service, re.MULTILINE)
        assert "    read_only: true" in service
        assert '    security_opt: ["no-new-privileges:true"]' in service
        assert "    depends_on:" not in service
        assert "default" not in service
        assert re.search(
            r"^    networks: \[" + ", ".join(expected_networks[service_name]) + r"\]$",
            service,
            re.MULTILINE,
        )

    assert "  data:\n    internal: true" in compose
    assert "  monitoring:\n    internal: true" in compose
    for service_name in MONITORING_SERVICES:
        assert "    ports:" not in _service_block(compose, service_name)

    postgres = _service_block(compose, "postgres_exporter")
    assert 'POSTGRES_EXPORTER_USER: "${POSTGRES_EXPORTER_USER:-}"' in postgres
    assert 'POSTGRES_EXPORTER_PASSWORD: "${POSTGRES_EXPORTER_PASSWORD:-}"' in postgres
    assert "DATA_SOURCE_USER: ${POSTGRES_USER}" not in postgres
    assert "DATA_SOURCE_PASS: ${POSTGRES_PASSWORD}" not in postgres
    assert "POSTGRES_EXPORTER_USER is required" in postgres
    assert "POSTGRES_EXPORTER_PASSWORD is required" in postgres
    assert "exec /bin/postgres_exporter" in postgres


def test_production_release_is_explicit_segmented_and_volume_safe():
    compose = (ROOT / "docker-compose-prod.yml").read_text()
    assert compose.startswith("name: vpn\n")
    assert "image: postgres:16@sha256:" in _service_block(compose, "db")
    assert "image: redis:7@sha256:" in _service_block(compose, "redis")

    profiles = {
        "admin": '["hub"]',
        "bot": '["bot"]',
        "rq_worker": '["worker"]',
        "rq_scheduler": '["billing-scheduler"]',
    }
    for service_name, profile in profiles.items():
        service = _service_block(compose, service_name)
        assert f"profiles: {profile}" in service
        assert "<<: *backend-security" in service
        assert "restart: unless-stopped" in service
        assert ":latest" not in service

    assert "networks: [data]" in _service_block(compose, "db")
    assert "networks: [data]" in _service_block(compose, "redis")
    assert "networks: [edge, app, data, monitoring]" in _service_block(compose, "admin")
    assert "networks: [app, data, monitoring]" in _service_block(compose, "bot")

    assert "external: true\n    name: vpn_db_data" in compose
    assert "external: true\n    name: vpn_redis_data" in compose
    assert "external: true\n    name: vpn_prometheus_data" in compose
    assert "REDIS_PASSWORD is required in production" in _service_block(
        compose, "redis"
    )
    assert '--requirepass "$$REDIS_PASSWORD"' in _service_block(compose, "redis")
    assert (
        "REDIS_URL: redis://:${REDIS_PASSWORD:?REDIS_PASSWORD is required}"
        "@redis:6379/0"
    ) in (_top_level_block(compose, "x-observability-environment"))

    required_images = {
        "migrations": "VPN_MIGRATIONS_IMAGE",
        "admin": "VPN_ADMIN_IMAGE",
        "bot": "VPN_BOT_IMAGE",
        "rq_worker": "VPN_BILLING_IMAGE",
        "rq_scheduler": "VPN_BILLING_IMAGE",
        "admin_frontend": "VPN_ADMIN_FRONTEND_IMAGE",
        "nginx": "VPN_NGINX_IMAGE",
    }
    for service_name, image_variable in required_images.items():
        service = _service_block(compose, service_name)
        assert ":latest" not in service
        assert "unreleased" not in service
        assert f"image: ${{{image_variable}:?{image_variable} is required}}" in service
    assert "env_file:" not in _service_block(compose, "admin_frontend")
    nginx = _service_block(compose, "nginx")
    assert '      - "127.0.0.1:14081:80"' in nginx
    assert '      - "14081:80"' not in nginx
    assert '      - "0.0.0.0:14081:80"' not in nginx

    release_policy = _top_level_block(compose, "x-release-policy-environment")
    for flag in (
        "MAINTENANCE_MODE",
        "BILLING_ENABLED",
        "PAYMENTS_ENABLED",
        "PROVISIONING_ENABLED",
        "NOTIFICATIONS_ENABLED",
        "REFERRAL_REWARDS_ENABLED",
    ):
        assert f"{flag}: ${{{flag}:?{flag} is required}}" in release_policy


def test_production_statsd_and_tls_mount_wiring_remains_fail_safe():
    compose = (ROOT / "docker-compose-prod.yml").read_text()
    local_compose = (ROOT / "docker-compose.yml").read_text()

    assert "source: ${VPN_MANAGER_TLS_DIR_PROD:-/etc/vpn-hub/manager-pki}" in compose
    assert "source: ${VPN_MANAGER_TLS_DIR:-./secrets/vpn-manager}" in local_compose
    assert "  read_only: true" in _top_level_block(
        compose,
        "x-vpn-manager-tls-volume",
    )
    for service_name in ("admin", "bot", "rq_worker"):
        service = _service_block(compose, service_name)
        assert "      - *vpn-manager-tls-volume" in service
        assert "*observability-environment" in service
        assert "*control-plane-environment" in service
        assert "      VPN_HUB_SERVICE:" in service

    control_plane = _top_level_block(compose, "x-control-plane-environment")
    assert 'VPN_MANAGER_TLS_ENABLED: "true"' in control_plane
    assert 'VPN_MANAGER_MTLS_REQUIRED: "true"' in control_plane
    assert "VPN_MANAGER_TLS_PORT: 16291" in control_plane
    assert "VPN_MANAGER_CA_CERT_PATH: /run/secrets/vpn-manager/ca.crt" in (
        control_plane
    )
    assert "VPN_MANAGER_CLIENT_CERT_PATH: /run/secrets/vpn-manager/client.crt" in (
        control_plane
    )
    assert "VPN_MANAGER_CLIENT_KEY_PATH: /run/secrets/vpn-manager/client.key" in (
        control_plane
    )


def test_nginx_explicitly_denies_operational_endpoints():
    config = (ROOT / "nginx/nginx.conf").read_text()

    for endpoint in ("metrics", "health", "ready"):
        match = re.search(
            rf"location\s*=\s*/{endpoint}\s*\{{(?P<body>.*?)\}}",
            config,
            flags=re.DOTALL,
        )
        assert match is not None
        body = match.group("body")
        assert "return 404;" in body
        assert "proxy_pass" not in body


def test_production_runbook_uses_an_explicit_maintenance_safe_service_list():
    documentation = (ROOT / "docs/observability.md").read_text()

    assert "statsd_exporter postgres_exporter redis_exporter prometheus" in (
        documentation
    )
    assert "Never run a generic `--profile monitoring up -d`" in documentation
    assert "intentionally have no `depends_on`" in documentation


def test_prometheus_contract_covers_exporters_and_p1_alerts():
    prometheus = (ROOT / "observability/prometheus.yml").read_text()
    for job in (
        "vpn-hub-admin",
        "vpn-hub-statsd",
        "vpn-hub-postgres",
        "vpn-hub-redis",
    ):
        assert f"job_name: {job}" in prometheus

    alerts = (ROOT / "observability/alerts.yml").read_text()
    assert "alert: VPNHubBillingNeverCompleted" in alerts
    assert "vpn_hub_billing_last_completed_timestamp_seconds == 0" in alerts
    assert "vpn_hub_observability_start_timestamp_seconds" in alerts
    assert "alert: VPNHubTelegramInboxBacklogOld" in alerts
    assert "alert: VPNHubTelegramInboxDead" in alerts
    assert "alert: VPNHubManagerTLSMaterialInvalid" in alerts
    assert "alert: VPNHubManagerTLSCertificateExpiring" in alerts


def _service_block(compose: str, service_name: str) -> str:
    match = re.search(
        rf"^  {re.escape(service_name)}:\n(?P<body>(?:^(?:    |$).*\n?)*)",
        compose,
        flags=re.MULTILINE,
    )
    assert match is not None, service_name
    return match.group(0)


def _top_level_block(compose: str, key: str) -> str:
    match = re.search(
        rf"^{re.escape(key)}:\s*&[^\n]+\n(?P<body>(?:^  .*\n?)*)",
        compose,
        flags=re.MULTILINE,
    )
    assert match is not None, key
    return match.group(0)
