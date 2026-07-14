from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from admin.app import app
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from core.config import settings
from core.db.models.billing_run import BillingRun
from core.db.models.notification_outbox import NotificationOutbox
from core.db.models.server import Server, VPNServerStatus
from core.db.models.telegram_update import TelegramUpdateInbox
from core.db.models.vpn_operation import VPNOperation
from core.db.schema import EXPECTED_ALEMBIC_REVISION
from core.observability.manager_tls import inspect_manager_tls_material
from core.observability.snapshot import (
    database_ready,
    dependency_readiness,
    render_prometheus_metrics,
)
from core.observability.statsd import StatsDClient
from core.services.api_gateway import APIGateway


def test_statsd_client_emits_bounded_dogstatsd_tags():
    client = StatsDClient(
        enabled=True,
        host="unused",
        port=9125,
        service="rq worker/one",
    )
    payloads: list[str] = []

    def capture(payload: str) -> bool:
        payloads.append(payload)
        return True

    client._send = capture  # type: ignore[method-assign]

    assert client.increment(
        "manager.requests",
        tags={"operation": "create/client", "outcome": "success"},
    )
    assert payloads == [
        "vpn_hub.manager.requests:1|c|#"
        "operation:create_client,outcome:success,service:rq_worker_one"
    ]


@pytest.mark.asyncio
async def test_database_readiness_requires_exact_alembic_head(sessionmaker):
    assert await database_ready() is False

    async with sessionmaker() as session, session.begin():
        await session.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        await session.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('stale')")
        )
    assert await database_ready() is False

    async with sessionmaker() as session, session.begin():
        await session.execute(
            text("UPDATE alembic_version SET version_num = :revision"),
            {"revision": EXPECTED_ALEMBIC_REVISION},
        )
    assert await database_ready() is True

    async with sessionmaker() as session, session.begin():
        await session.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('extra-head')")
        )
    assert await database_ready() is False


def test_readiness_revision_matches_the_only_alembic_head():
    script = ScriptDirectory.from_config(AlembicConfig("alembic.ini"))
    assert set(script.get_heads()) == {EXPECTED_ALEMBIC_REVISION}


@pytest.mark.asyncio
async def test_dependency_readiness_includes_manager_tls(monkeypatch):
    async def ready():
        return True

    async def tls_not_ready():
        return False

    monkeypatch.setattr("core.observability.snapshot.database_ready", ready)
    monkeypatch.setattr("core.observability.snapshot.redis_ready", ready)
    monkeypatch.setattr(
        "core.observability.snapshot.manager_tls_ready",
        tls_not_ready,
    )

    assert await dependency_readiness() == {
        "database": True,
        "redis": True,
        "manager_tls": False,
    }


@pytest.mark.asyncio
async def test_manager_observation_normalizes_path_and_counts_retries(monkeypatch):
    calls: list[dict] = []

    def observe(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("core.services.api_gateway.observe_manager_request", observe)
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503 if attempts == 1 else 200, content=b"vpn")

    async def no_sleep(delay: float) -> None:
        return None

    gateway = APIGateway(
        "127.0.0.1",
        8080,
        "secret",
        retries=1,
        backoff=0,
        jitter=0,
        sleep=no_sleep,
    )
    gateway._client = httpx.AsyncClient(
        base_url="http://manager.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        response = await gateway._request("GET", "/clients/private-name/config")
    finally:
        await gateway._client.aclose()

    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0]["operation"] == "download_config"
    assert calls[0]["outcome"] == "success"
    assert calls[0]["attempts"] == 2
    assert "private-name" not in str(calls[0])


@pytest.mark.asyncio
async def test_database_metrics_report_backlog_age_and_outbox_lag(sessionmaker):
    now = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    async with sessionmaker() as session, session.begin():
        session.add(
            BillingRun(
                period_key="metrics-period",
                period_start=now - timedelta(hours=1),
                period_end=now,
                cost_per_config=Decimal("1.00"),
                status="completed",
                charged_users=2,
                total_amount=Decimal("4.00"),
                completed_at=now - timedelta(minutes=10),
            )
        )
        session.add(
            VPNOperation(
                operation_id=str(uuid4()),
                config_id=None,
                config_name="must-not-be-a-label",
                server_id=None,
                owner_id=None,
                kind="provision",
                status="failed",
                created_at=now - timedelta(minutes=20),
            )
        )
        session.add(
            NotificationOutbox(
                dedupe_key="metrics-outbox",
                chat_id=123,
                text="private text",
                status="pending",
                attempts=2,
                created_at=now - timedelta(minutes=15),
            )
        )
        session.add(
            NotificationOutbox(
                dedupe_key="metrics-outbox-stale-queued",
                chat_id=456,
                text="queued private text",
                status="queued",
                created_at=now - timedelta(hours=2),
                published_at=now - timedelta(minutes=20),
            )
        )
        session.add(
            TelegramUpdateInbox(
                update_id=1001,
                payload={"message": {"text": "telegram-private-text"}},
                source="polling",
                ordering_key="v1:metrics-failed",
                status="failed",
                attempts=2,
                received_at=now - timedelta(minutes=25),
            )
        )
        session.add(
            TelegramUpdateInbox(
                update_id=1002,
                payload={"callback_query": {"data": "telegram-private-data"}},
                source="polling",
                ordering_key="v1:metrics-dead",
                status="dead",
                attempts=20,
                received_at=now - timedelta(minutes=30),
            )
        )
        server = Server(
            name="metrics-fleet-node",
            ip="192.0.2.50",
            port=16290,
            host="metrics-fleet-node.example.test",
            monthly_cost=Decimal("10.00"),
            location="NL",
            api_key="metrics-manager-secret",
            lifecycle_state="active",
            accepts_new_configs=True,
            max_configs=10,
            capacity_reserve=2,
            manager_instance_id="56c1ab62-0c42-4f03-83c6-4c8e6c43e29b",
        )
        session.add(server)
        await session.flush()
        session.add(
            VPNServerStatus(
                server_id=server.id,
                kind="status",
                success=True,
                manager_instance_id=server.manager_instance_id,
                collected_at=now - timedelta(seconds=60),
                snapshot={
                    "readiness": {"ready": True, "errors": []},
                    "data_plane": {
                        "status": "up",
                        "online_sessions": 3,
                    },
                    "pki": {
                        "server_certificate": {
                            "status": "expiring",
                            "expires_at": (now + timedelta(days=20)).isoformat(),
                        }
                    },
                },
            )
        )

    payload = await render_prometheus_metrics(redis_is_ready=False, now=now)

    assert 'vpn_hub_dependency_ready{dependency="redis"} 0' in payload
    assert 'vpn_hub_feature_enabled{feature="payments"} 1' in payload
    assert 'vpn_hub_vpn_operations{status="failed"} 1' in payload
    assert "vpn_hub_vpn_operation_oldest_backlog_age_seconds 1200" in payload
    assert 'vpn_hub_notification_outbox{status="pending"} 1' in payload
    assert 'vpn_hub_notification_outbox{status="queued"} 1' in payload
    assert 'vpn_hub_notification_outbox{status="unknown"} 0' in payload
    assert "vpn_hub_notification_outbox_oldest_pending_age_seconds 900" in payload
    assert "vpn_hub_notification_outbox_backlog 2" in payload
    assert "vpn_hub_notification_outbox_oldest_backlog_age_seconds 1200" in payload
    assert "vpn_hub_notification_outbox_retrying 1" in payload
    assert 'vpn_hub_telegram_update_inbox{status="failed"} 1' in payload
    assert 'vpn_hub_telegram_update_inbox{status="dead"} 1' in payload
    assert "vpn_hub_telegram_update_inbox_backlog 1" in payload
    assert "vpn_hub_telegram_update_inbox_oldest_backlog_age_seconds 1500" in payload
    assert "vpn_hub_telegram_update_inbox_dead 1" in payload
    assert 'vpn_hub_fleet_servers{lifecycle="active"} 1' in payload
    assert 'vpn_hub_fleet_server_health{status="healthy"} 1' in payload
    assert "vpn_hub_fleet_servers_missing_status 0" in payload
    assert "vpn_hub_fleet_oldest_status_age_seconds 60" in payload
    assert "vpn_hub_fleet_online_sessions 3" in payload
    assert "vpn_hub_fleet_capacity_total 10" in payload
    assert "vpn_hub_fleet_capacity_available 8" in payload
    assert "vpn_hub_fleet_servers_at_capacity 0" in payload
    assert "metrics-manager-secret" not in payload
    assert "must-not-be-a-label" not in payload
    assert "private text" not in payload
    assert "telegram-private" not in payload


@pytest.mark.asyncio
async def test_manager_tls_material_is_fail_closed_and_reports_expiry(
    monkeypatch,
    tmp_path: Path,
    sessionmaker,
):
    now = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
    cert_path, key_path = _write_certificate(
        tmp_path,
        not_before=now - timedelta(days=30),
        not_after=now - timedelta(minutes=1),
    )
    monkeypatch.setattr(settings, "vpn_manager_tls_enabled", True)
    monkeypatch.setattr(settings, "vpn_manager_ca_cert_path", "")
    monkeypatch.setattr(settings, "vpn_manager_client_cert_path", str(cert_path))
    monkeypatch.setattr(settings, "vpn_manager_client_key_path", str(key_path))

    expired = inspect_manager_tls_material(now=now)

    assert expired.enabled is True
    assert expired.ready is False
    assert expired.certificate_not_after[0][0] == "client"
    payload = await render_prometheus_metrics(
        redis_is_ready=True,
        tls_status=expired,
        now=now,
    )
    assert "vpn_hub_manager_tls_enabled 1" in payload
    assert "vpn_hub_manager_tls_material_ready 0" in payload
    assert (
        'vpn_hub_manager_tls_certificate_expiry_timestamp_seconds{certificate="client"}'
        in payload
    )

    monkeypatch.setattr(
        settings,
        "vpn_manager_client_cert_path",
        str(tmp_path / "missing-client.crt"),
    )
    missing = inspect_manager_tls_material(now=now)
    assert missing.ready is False


@pytest.mark.asyncio
async def test_observability_routes_are_public_and_readiness_is_explicit(monkeypatch):
    async def dependencies():
        return {"database": True, "redis": False}

    monkeypatch.setattr(
        "admin.routers.observability.dependency_readiness", dependencies
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/health")
        readiness = await client.get("/ready")

    assert health.status_code == 200
    assert readiness.status_code == 503
    assert readiness.json()["dependencies"] == {"database": True, "redis": False}
    assert readiness.json()["payments_enabled"] is True


def _write_certificate(
    directory: Path,
    *,
    not_before: datetime,
    not_after: datetime,
) -> tuple[Path, Path]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "vpn-hub-observability-test")]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .sign(private_key, hashes.SHA256())
    )
    cert_path = directory / "client.crt"
    key_path = directory / "client.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path
