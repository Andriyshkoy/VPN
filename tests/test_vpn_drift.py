from __future__ import annotations

import pytest

from core.config import settings
from core.db.unit_of_work import uow
from core.domain import VPNOperationKind, VPNOperationStatus, VPNState
from core.exceptions import InvalidOperationError
from core.services import ServerService, UserService
from core.services.api_gateway import ManagerClientInventory, ManagerClientState
from core.services.config import ConfigService
from core.services.vpn_drift import VPNDriftService


def manager_client(
    name: str,
    state: str,
    *,
    manageable: bool = True,
    issues: tuple[str, ...] = (),
) -> ManagerClientState:
    active_like = state in {"active", "suspended"}
    certificate_status = {
        "revoked": "revoked",
        "expired": "expired",
        "orphaned": "missing",
        "unknown": "unknown",
    }.get(state, "valid")
    return ManagerClientState(
        name=name,
        state=state,
        certificate_status=certificate_status,
        index_statuses=("V",) if active_like else (),
        suspended=state == "suspended",
        config_present=active_like,
        config_complete=active_like,
        certificate_present=active_like,
        private_key_present=active_like,
        manageable=manageable,
        issues=issues,
    )


def inventory(
    *clients: ManagerClientState,
    revision: str = "sha256:reviewed",
) -> ManagerClientInventory:
    return ManagerClientInventory(
        revision=revision,
        count=len(clients),
        clients=clients,
        etag=f'"{revision}"',
    )


class InventoryGateway:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.etags: list[str | None] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get_client_inventory(self, *, etag=None):
        self.etags.append(etag)
        if isinstance(self.snapshot, list):
            return self.snapshot.pop(0)
        return self.snapshot


class MutationGateway:
    def __init__(self):
        self.unsuspended: list[tuple[str, str | None]] = []
        self.suspended: list[tuple[str, str | None]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def unsuspend_client(self, name, *, operation_id=None):
        self.unsuspended.append((name, operation_id))

    async def suspend_client(self, name, *, operation_id=None):
        self.suspended.append((name, operation_id))


async def create_hub_state(
    *,
    name: str,
    desired_state: str = VPNState.ACTIVE.value,
    actual_state: str = VPNState.ACTIVE.value,
):
    user = await UserService(uow).register(700_000 + create_hub_state.counter)
    create_hub_state.counter += 1
    server = await ServerService(uow).create(
        name=f"drift-{name}",
        ip="manager.test",
        port=16290,
        host="vpn.test",
        location="test",
        api_key="secret",
        cost=0,
    )
    async with uow() as repos:
        config = await repos["configs"].create(
            server.id,
            user.id,
            name,
            name,
            desired_state=desired_state,
            actual_state=actual_state,
        )
    return server, config


create_hub_state.counter = 1


@pytest.mark.asyncio
async def test_audit_is_read_only_and_reports_safe_and_unsafe_drift(sessionmaker):
    server, config = await create_hub_state(name="known-active")
    gateway = InventoryGateway(
        inventory(
            manager_client("known-active", "suspended"),
            manager_client("remote-live", "active"),
            manager_client("old-history", "revoked"),
        )
    )
    service = VPNDriftService(
        uow,
        gateway_factory=lambda *args, **kwargs: gateway,
    )

    report = await service.audit_server(server.id)

    assert report.inventory_revision == "sha256:reviewed"
    assert report.unchanged is False
    by_name = {finding.name: finding for finding in report.findings}
    mismatch = by_name["known-active"]
    assert mismatch.config_id == config.id
    assert mismatch.reason == "remote_state_mismatch"
    assert mismatch.repairable is True
    assert by_name["remote-live"].reason == "remote_only_live"
    assert by_name["remote-live"].severity == "critical"
    assert by_name["remote-live"].repairable is False
    assert by_name["old-history"].reason == "remote_only_inert"

    async with uow() as repos:
        assert await repos["vpn_operations"].list() == []


@pytest.mark.asyncio
async def test_etag_304_still_compares_fresh_hub_state(sessionmaker):
    server, _ = await create_hub_state(name="etag-client")
    gateway = InventoryGateway(
        [
            None,
            inventory(manager_client("etag-client", "suspended")),
        ]
    )
    service = VPNDriftService(
        uow,
        gateway_factory=lambda *args, **kwargs: gateway,
    )

    report = await service.audit_server(server.id, etag='"sha256:old"')

    assert report.unchanged is True
    assert [finding.reason for finding in report.findings] == ["remote_state_mismatch"]
    assert gateway.etags == ['"sha256:old"', None]


@pytest.mark.asyncio
async def test_integrity_and_missing_states_are_never_auto_repaired(sessionmaker):
    server, first = await create_hub_state(name="broken")
    async with uow() as repos:
        user = await repos["users"].get(id=first.owner_id)
        missing = await repos["configs"].create(
            server.id,
            user.id,
            "missing",
            "Missing",
            desired_state=VPNState.SUSPENDED.value,
            actual_state=VPNState.SUSPENDED.value,
        )
    gateway = InventoryGateway(
        inventory(
            manager_client(
                "broken",
                "active",
                issues=("multiple_valid_index_records",),
            )
        )
    )
    service = VPNDriftService(
        uow,
        gateway_factory=lambda *args, **kwargs: gateway,
    )

    report = await service.audit_server(server.id)
    by_id = {
        finding.config_id: finding
        for finding in report.findings
        if finding.config_id is not None
    }

    assert by_id[first.id].reason == "manager_integrity_issue"
    assert by_id[first.id].repairable is False
    assert by_id[missing.id].reason == "remote_missing"
    assert by_id[missing.id].repairable is False


@pytest.mark.asyncio
async def test_repair_requires_opt_in_and_exact_reviewed_revision(
    monkeypatch,
    sessionmaker,
):
    server, config = await create_hub_state(name="review-guard")
    gateway = InventoryGateway(inventory(manager_client("review-guard", "suspended")))
    service = VPNDriftService(
        uow,
        gateway_factory=lambda *args, **kwargs: gateway,
    )

    monkeypatch.setattr(settings, "vpn_drift_repair_enabled", False)
    with pytest.raises(InvalidOperationError, match="not enabled"):
        await service.repair_server(
            server.id,
            expected_revision="sha256:reviewed",
            config_ids=[config.id],
        )

    monkeypatch.setattr(settings, "vpn_drift_repair_enabled", True)
    with pytest.raises(InvalidOperationError, match="changed"):
        await service.repair_server(
            server.id,
            expected_revision="sha256:stale",
            config_ids=[config.id],
        )


@pytest.mark.asyncio
async def test_safe_repair_uses_fenced_durable_operation(
    monkeypatch,
    sessionmaker,
):
    server, config = await create_hub_state(name="repair-active")
    inventory_gateway = InventoryGateway(
        inventory(manager_client("repair-active", "suspended"))
    )
    mutation_gateway = MutationGateway()
    monkeypatch.setattr(settings, "vpn_drift_repair_enabled", True)
    monkeypatch.setattr(settings, "maintenance_mode", False)
    monkeypatch.setattr(settings, "provisioning_enabled", True)
    monkeypatch.setattr(
        "core.services.config.APIGateway",
        lambda *args, **kwargs: mutation_gateway,
    )
    service = VPNDriftService(
        uow,
        config_service=ConfigService(uow),
        gateway_factory=lambda *args, **kwargs: inventory_gateway,
    )

    repaired = await service.repair_server(
        server.id,
        expected_revision="sha256:reviewed",
        config_ids=[config.id],
    )

    assert repaired.completed == 1
    assert len(repaired.operations) == 1
    operation_id = repaired.operations[0].operation_id
    assert mutation_gateway.unsuspended == [("repair-active", operation_id)]
    async with uow() as repos:
        operation = await repos["vpn_operations"].get(operation_id=operation_id)
        row = await repos["configs"].get(id=config.id)
    assert operation.kind == VPNOperationKind.UNSUSPEND.value
    assert operation.status == VPNOperationStatus.SUCCEEDED.value
    assert row.desired_state == VPNState.ACTIVE.value
    assert row.actual_state == VPNState.ACTIVE.value


@pytest.mark.asyncio
async def test_repair_rejects_remote_only_and_unselected_findings(
    monkeypatch,
    sessionmaker,
):
    server, config = await create_hub_state(name="in-sync")
    gateway = InventoryGateway(
        inventory(
            manager_client("in-sync", "active"),
            manager_client("remote-only", "active"),
        )
    )
    monkeypatch.setattr(settings, "vpn_drift_repair_enabled", True)
    service = VPNDriftService(
        uow,
        gateway_factory=lambda *args, **kwargs: gateway,
    )

    with pytest.raises(InvalidOperationError, match="not currently safe"):
        await service.repair_server(
            server.id,
            expected_revision="sha256:reviewed",
            config_ids=[config.id],
        )
