from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from core.config import settings
from core.db.models import LedgerEntry, VPNAccessBinding, VPNOperation, VPNServerStatus
from core.db.unit_of_work import uow
from core.exceptions import (
    APIAuthenticationError,
    APIConfigurationError,
    APIConnectionError,
    APITransportError,
    ConfigNotFoundError,
    InvalidOperationError,
)
from core.services import ConfigService, ServerService, UserService
from core.services.node_gateway import NodeAccess, NodeProfile
from core.services.node_policy import normalize_node_policy


class MemoryNode:
    """Remote idempotency journal, intentionally independent of Hub transactions."""

    def __init__(self):
        self.accesses = {}
        self.creates = {}
        self.calls = []
        self.timeout_after_create = False
        self.auth_failure = False
        self.observed_after_create = "active"
        self.control_verified = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def access(self, access_id):
        row = self.accesses[access_id]
        return NodeAccess(
            id=access_id,
            node_id="nl-1",
            service_id=row["service_id"],
            status=row["status"],
            desired_status=row["desired_status"],
            settings=copy.deepcopy(row["policy"]),
            effective=None,
            revision=1,
            operation_pending=False,
            control_plane_verified=self.control_verified,
        )

    async def create_access(self, *, service_id, owner_ref, policy, operation_id):
        body = {"service_id": service_id, "owner_ref": owner_ref, "policy": policy}
        self.calls.append(("create", operation_id, copy.deepcopy(body)))
        if self.auth_failure:
            raise APIAuthenticationError("credentials rejected", status_code=401)
        if operation_id in self.creates:
            old_body, access_id = self.creates[operation_id]
            assert old_body == body, "a retry changed the remote request"
        else:
            access_id = str(uuid.uuid4())
            self.creates[operation_id] = copy.deepcopy(body), access_id
            self.accesses[access_id] = {
                **copy.deepcopy(body),
                "status": self.observed_after_create,
                "desired_status": "active",
            }
        if self.timeout_after_create:
            self.timeout_after_create = False
            raise APITransportError("response lost after remote creation")
        return self.access(access_id)

    async def get_access(self, access_id):
        return self.access(access_id)

    async def _transition(self, access_id, state, operation_id):
        self.calls.append((state, operation_id, access_id))
        self.accesses[access_id]["status"] = state
        self.accesses[access_id]["desired_status"] = state
        return self.access(access_id)

    async def suspend_access(self, access_id, *, operation_id):
        return await self._transition(access_id, "suspended", operation_id)

    async def resume_access(self, access_id, *, operation_id):
        return await self._transition(access_id, "active", operation_id)

    async def revoke_access(self, access_id, *, operation_id):
        return await self._transition(access_id, "revoked", operation_id)

    async def export_profile(self, access_id):
        return NodeProfile(
            access_id=access_id,
            service_id=self.accesses[access_id]["service_id"],
            uri="vless://test-only-secret@example.invalid:443#test",
        )


async def setup_node(monkeypatch, *, clock=None):
    monkeypatch.setattr(settings, "maintenance_mode", False)
    monkeypatch.setattr(settings, "provisioning_enabled", True)
    node = MemoryNode()
    monkeypatch.setattr(
        "core.services.config.ConfigService._create_node_gateway",
        staticmethod(lambda *args, **kwargs: node),
    )
    user = await UserService(uow).register(912346)
    server = await ServerService(uow).create(
        name="node",
        ip="127.0.0.1",
        port=18010,
        host="nl.test",
        location="NL",
        api_key="test-token",
        cost=0,
        backend_type="node-agent",
        node_id="nl-1",
        lifecycle_state="active",
        accepts_new_configs=True,
    )
    async with uow() as repos:
        repos["servers"].session.add(
            VPNServerStatus(
                server_id=server.id,
                kind="status",
                success=True,
                collected_at=clock() if clock else datetime.now(timezone.utc),
                snapshot={
                    "backend_type": "node-agent",
                    "node_id": "nl-1",
                    "endpoint_revision": 1,
                    "readiness": {"ready": True},
                    "data_plane": {"status": "unknown"},
                },
            )
        )
    return node, user, server, ConfigService(uow, clock=clock)


def request(user, server, *, service_id="vless-reality", operation_id=None):
    return dict(
        server_id=server.id,
        owner_id=user.id,
        name="node-device",
        display_name="Device",
        service_id=service_id,
        access_policy={"traffic_limit_bytes": 1024**3},
        operation_id=operation_id or str(uuid.uuid4()),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("service_id", ["hysteria", "vless-reality"])
async def test_node_lifecycle_preserves_identity_and_tombstone_without_money(
    monkeypatch,
    sessionmaker,
    service_id,
):
    node, user, server, service = await setup_node(monkeypatch)
    args = request(user, server, service_id=service_id)
    cfg = await service.create_config(**args)
    assert cfg.remote_access_id and cfg.service_id == service_id
    assert (await service.create_config(**args)).id == cfg.id
    before = await service.export_profile(cfg.id)
    with pytest.raises(InvalidOperationError):
        await service.download_config(cfg.id)
    assert (await service.suspend_config(cfg.id)).actual_state == "suspended"
    assert (await service.unsuspend_config(cfg.id)).actual_state == "active"
    assert (await service.export_profile(cfg.id)).uri == before.uri
    await service.revoke_config(cfg.id)
    assert await service.get(cfg.id) is None
    with pytest.raises((InvalidOperationError, ConfigNotFoundError)):
        await service.create_config(**args)
    async with uow() as repos:
        binding = await repos["vpn_access_bindings"].get_for_config_ref(cfg.id)
        assert binding.remote_access_id == cfg.remote_access_id
        assert binding.observed_state == "revoked" and binding.revoked_at is not None
        assert (
            await repos["users"].session.scalar(
                select(func.count()).select_from(LedgerEntry)
            )
            == 0
        )
        stored = await repos["vpn_operations"].list()
        assert before.uri not in repr([op.payload for op in stored])
    assert len(node.accesses) == 1
    assert node.calls[0][1] == args["operation_id"]


@pytest.mark.asyncio
async def test_lost_create_response_replays_original_uuid_and_immutable_body(
    monkeypatch, sessionmaker
):
    now = [datetime.now(timezone.utc)]
    node, user, server, service = await setup_node(monkeypatch, clock=lambda: now[0])
    node.timeout_after_create = True
    args = request(user, server)
    with pytest.raises(APITransportError):
        await service.create_config(**args)
    async with uow() as repos:
        binding = (await repos["vpn_access_bindings"].list())[0]
        assert binding.remote_access_id is None
        config_id = binding.config_id
    now[0] += timedelta(seconds=6)
    assert (await service.reconcile())[config_id] == "succeeded"
    cfg = await service.get(config_id)
    assert len(node.accesses) == 1 and cfg.remote_access_id in node.accesses
    assert node.calls[0] == node.calls[1]
    changed = {**args, "access_policy": {"traffic_limit_bytes": 2 * 1024**3}}
    with pytest.raises(InvalidOperationError):
        await service.create_config(**changed)
    assert len(node.accesses) == 1


@pytest.mark.asyncio
async def test_crash_after_remote_success_before_binding_commit_recovers(
    monkeypatch, sessionmaker
):
    now = [datetime.now(timezone.utc)]
    node, user, server, service = await setup_node(monkeypatch, clock=lambda: now[0])
    complete = service._complete_success

    async def crash(*args, **kwargs):
        raise RuntimeError("process died before the completion transaction")

    service._complete_success = crash
    with pytest.raises(RuntimeError):
        await service.create_config(**request(user, server))
    assert len(node.accesses) == 1
    now[0] += timedelta(seconds=121)
    service._complete_success = complete
    assert list((await service.reconcile()).values()) == ["succeeded"]
    async with uow() as repos:
        binding = (await repos["vpn_access_bindings"].list())[0]
        assert binding.remote_access_id == next(iter(node.accesses))
    assert node.calls[0] == node.calls[1]


@pytest.mark.asyncio
async def test_stale_lease_cannot_attach_remote_uuid(monkeypatch, sessionmaker):
    now = [datetime.now(timezone.utc)]
    node, user, server, service = await setup_node(monkeypatch, clock=lambda: now[0])
    args = request(user, server)
    async with uow() as repos:
        await service.prepare_config(repos=repos, **args)
    old, _ = await service._claim_context(args["operation_id"])
    remote = await service._execute_node_operation(old)
    now[0] += timedelta(seconds=121)
    current, _ = await service._claim_context(args["operation_id"])
    assert (
        await service._complete_success(old, "active", node_access=remote)
        == "superseded"
    )
    async with uow() as repos:
        assert (
            await repos["vpn_access_bindings"].get_for_config(old.config_id)
        ).remote_access_id is None
    assert (
        await service._complete_success(current, "active", node_access=remote)
        == "succeeded"
    )
    async with uow() as repos:
        assert (
            await repos["vpn_access_bindings"].get_for_config(old.config_id)
        ).remote_access_id == remote.id


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["expired", "limited"])
async def test_node_observed_policy_state_is_not_active_or_financial_suspension(
    monkeypatch, sessionmaker, state
):
    node, user, server, service = await setup_node(monkeypatch)
    node.observed_after_create = state
    cfg = await service.create_config(**request(user, server))
    assert cfg.actual_state == state and cfg.desired_state == "active"
    async with uow() as repos:
        assert (
            await repos["vpn_access_bindings"].get_for_config(cfg.id)
        ).observed_state == state
        assert (
            await repos["vpn_operations"].session.scalar(
                select(func.count()).select_from(VPNOperation)
            )
            == 1
        )


@pytest.mark.asyncio
async def test_node_auth_failure_keeps_recovery_identity_and_operation_retryable(
    monkeypatch, sessionmaker
):
    node, user, server, service = await setup_node(monkeypatch)
    node.auth_failure = True
    args = request(user, server)
    with pytest.raises(APIConnectionError):
        await service.create_config(**args)
    async with uow() as repos:
        op = await repos["vpn_operations"].get(operation_id=args["operation_id"])
        assert op.status == "failed"
        assert (await repos["vpn_access_bindings"].list())[
            0
        ].provision_operation_id == args["operation_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,verified", [("failed", True), ("revoked", True), ("active", False)]
)
async def test_unverified_or_failed_node_provision_is_not_completed(
    monkeypatch, sessionmaker, state, verified
):
    node, user, server, service = await setup_node(monkeypatch)
    node.observed_after_create, node.control_verified = state, verified
    args = request(user, server)
    with pytest.raises(APIConnectionError):
        await service.create_config(**args)
    async with uow() as repos:
        op = await repos["vpn_operations"].get(operation_id=args["operation_id"])
        assert op.status == "failed"
        binding = (await repos["vpn_access_bindings"].list())[0]
        assert binding.remote_access_id is None
        assert binding.provision_operation_id == args["operation_id"]


@pytest.mark.asyncio
async def test_revoke_after_ambiguous_exhausted_provision_recovers_original_identity(
    monkeypatch, sessionmaker
):
    monkeypatch.setattr(settings, "vpn_operation_max_attempts", 1)
    node, user, server, service = await setup_node(monkeypatch)
    node.timeout_after_create = True
    args = request(user, server)
    with pytest.raises(APITransportError):
        await service.create_config(**args)
    async with uow() as repos:
        binding = (await repos["vpn_access_bindings"].list())[0]
        cfg_id = binding.config_id
        assert (
            await repos["vpn_operations"].get(operation_id=args["operation_id"])
        ).status == "exhausted"
    await service.revoke_config(cfg_id)
    assert await service.get(cfg_id) is None
    async with uow() as repos:
        binding = await repos["vpn_access_bindings"].get_for_config_ref(cfg_id)
        assert binding.remote_access_id == next(iter(node.accesses))
        assert binding.revoked_at and binding.observed_state == "revoked"
    assert len(node.accesses) == 1 and node.calls[0] == node.calls[1]


@pytest.mark.asyncio
async def test_profile_service_mismatch_is_rejected(monkeypatch, sessionmaker):
    node, user, server, service = await setup_node(monkeypatch)
    cfg = await service.create_config(**request(user, server))
    node.accesses[cfg.remote_access_id]["service_id"] = "hysteria"
    with pytest.raises(APIConfigurationError):
        await service.export_profile(cfg.id)


@pytest.mark.parametrize(
    "service_id,policy",
    [
        ("hysteria", {"expires_at": "2030-01-01T00:00:00Z"}),
        ("vless-reality", {"activation_days": 7}),
        ("vless-reality", {"traffic_limit_bytes": True}),
        ("vless-reality", {"expires_at": "2030-01-01T00:00:00"}),
        ("vless-reality", {"unknown": "value"}),
    ],
)
def test_invalid_node_policy_is_rejected_without_rounding(service_id, policy):
    with pytest.raises(InvalidOperationError):
        normalize_node_policy(service_id, policy)
