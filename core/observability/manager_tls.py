from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509

from core.config import settings


@dataclass(frozen=True, slots=True)
class ManagerTLSStatus:
    """Bounded, non-secret result of validating configured Manager TLS files."""

    enabled: bool
    ready: bool
    certificate_not_after: tuple[tuple[str, datetime], ...] = ()


def inspect_manager_tls_material(
    *,
    now: datetime | None = None,
) -> ManagerTLSStatus:
    """Validate readability, certificate windows, and client key matching.

    Empty paths intentionally retain the APIGateway contract: system trust is
    allowed and a client identity is optional. Once a path is configured it is
    fail-closed for readiness.
    """

    if not settings.vpn_manager_tls_enabled:
        return ManagerTLSStatus(enabled=False, ready=True)

    current_time = _as_utc(now or datetime.now(timezone.utc))
    ca_path = settings.vpn_manager_ca_cert_path.strip()
    client_cert_path = settings.vpn_manager_client_cert_path.strip()
    client_key_path = settings.vpn_manager_client_key_path.strip()
    if bool(client_cert_path) != bool(client_key_path):
        return ManagerTLSStatus(enabled=True, ready=False)

    certificates: list[tuple[str, datetime]] = []
    valid_windows = True
    try:
        context = ssl.create_default_context(cafile=ca_path or None)
        if ca_path:
            ca_certificates = _load_pem_certificates(ca_path)
            valid_windows &= all(
                _certificate_is_current(certificate, current_time)
                for certificate in ca_certificates
            )
            certificates.append(
                (
                    "ca",
                    min(
                        _not_valid_after(certificate) for certificate in ca_certificates
                    ),
                )
            )

        if client_cert_path and client_key_path:
            context.load_cert_chain(
                certfile=client_cert_path,
                keyfile=client_key_path,
            )
            client_chain = _load_pem_certificates(client_cert_path)
            valid_windows &= all(
                _certificate_is_current(certificate, current_time)
                for certificate in client_chain
            )
            certificates.append(("client", _not_valid_after(client_chain[0])))
    except Exception:
        return ManagerTLSStatus(enabled=True, ready=False)

    return ManagerTLSStatus(
        enabled=True,
        ready=valid_windows,
        certificate_not_after=tuple(certificates),
    )


async def manager_tls_ready() -> bool:
    try:
        async with asyncio.timeout(settings.readiness_timeout_seconds):
            status = await asyncio.to_thread(inspect_manager_tls_material)
        return status.ready
    except Exception:
        return False


def _load_pem_certificates(path: str) -> list[x509.Certificate]:
    payload = Path(path).read_bytes()
    certificates = x509.load_pem_x509_certificates(payload)
    if not certificates:
        raise ValueError("TLS certificate file contains no certificates")
    return certificates


def _certificate_is_current(
    certificate: x509.Certificate,
    now: datetime,
) -> bool:
    return _not_valid_before(certificate) <= now < _not_valid_after(certificate)


def _not_valid_before(certificate: x509.Certificate) -> datetime:
    value = getattr(certificate, "not_valid_before_utc", None)
    if value is None:  # pragma: no cover - compatibility with old cryptography
        value = certificate.not_valid_before.replace(tzinfo=timezone.utc)
    return _as_utc(value)


def _not_valid_after(certificate: x509.Certificate) -> datetime:
    value = getattr(certificate, "not_valid_after_utc", None)
    if value is None:  # pragma: no cover - compatibility with old cryptography
        value = certificate.not_valid_after.replace(tzinfo=timezone.utc)
    return _as_utc(value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
