from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_SERVICES = (
    "db",
    "redis",
    "migrations",
    "admin",
    "bot",
    "rq_worker",
    "rq_scheduler",
    "statsd_exporter",
    "postgres_exporter",
    "redis_exporter",
    "prometheus",
    "admin_frontend",
    "nginx",
)


def test_every_production_service_has_bounded_json_file_logs():
    compose = (ROOT / "docker-compose-prod.yml").read_text()
    logging_extension = _top_level_block(compose, "x-json-file-logging")

    assert "driver: json-file" in logging_extension
    assert 'max-size: "10m"' in logging_extension
    assert 'max-file: "5"' in logging_extension

    for service_name in PRODUCTION_SERVICES:
        assert "logging: *json-file-logging" in _service_block(
            compose,
            service_name,
        )


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
