"""Offline retirement preserves history without claiming a remote revoke."""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from core.config import settings
from core.db.models import (
    AdminAuditEvent,
    AdminUser,
    LedgerEntry,
    Server,
    Subscription,
    SubscriptionAccess,
    User,
    VPN_Config,
    VPNAccessBinding,
    VPNOperation,
)
from core.db.unit_of_work import uow
from core.exceptions import InvalidOperationError
from core.services.subscriptions import SubscriptionService
from scripts.retire_openvpn import ACTION, OpenVPNRetirement
from tests.test_node_lifecycle import setup_node

REASON = "Owner confirms the KZ installation was permanently shut down"


def server_row(*, node=False):
    return Server(
        name="NL" if node else "Legacy KZ",
        ip="127.0.0.1",
        port=18010,
        host="nl.invalid" if node else "kz.invalid",
        location="NL" if node else "KZ",
        api_key="not-a-real-manager-token",
        monthly_cost=0,
        backend_type="node-agent" if node else "openvpn-manager",
        node_id="nl-1" if node else None,
        lifecycle_state="active",
        accepts_new_configs=True,
    )


async def seed_legacy(sessionmaker, *, user_id=None, status="suspended", plan="basic"):
    async with sessionmaker() as session:
        # Test the production FK semantics; the shared SQLite fixture leaves
        # these disabled by default.
        await session.execute(text("PRAGMA foreign_keys=ON"))
        assert await session.scalar(text("PRAGMA foreign_keys")) == 1
        actor = AdminUser(username="owner", password_hash="unused", role="owner")
        server = server_row()
        session.add_all([actor, server])
        if user_id is None:
            user = User(
                tg_id=1234567, username="old-customer", balance=Decimal("99.93")
            )
            session.add(user)
        else:
            user = await session.get(User, user_id)
            user.balance = Decimal("99.93")
        await session.flush()
        cfg = VPN_Config(
            name="old-opaque-manager-identity",
            display_name="Old phone",
            server_id=server.id,
            owner_id=user.id,
            suspended=True,
            desired_state="suspended",
            actual_state="suspended",
            operation_id=str(uuid.uuid4()),
        )
        session.add(cfg)
        await session.flush()
        payload = {"historical": "do not rewrite", "use_password": False}
        operation = VPNOperation(
            operation_id=cfg.operation_id,
            config_id=cfg.id,
            config_name=cfg.name,
            server_id=server.id,
            owner_id=user.id,
            kind="suspend",
            status="succeeded",
            attempts=1,
            payload=payload,
        )
        sub = Subscription(
            user_id=user.id,
            plan_code=plan,
            status=status,
            version=3,
            migration_source="historical-product-rollout",
            migration_snapshot={
                "legacy_config_ids": [cfg.id],
                "cutover_state": "pending",
                "historic_extra": "keep",
            },
        )
        ledger = LedgerEntry(
            user_id=user.id,
            amount=Decimal("99.93"),
            balance_after=Decimal("99.93"),
            kind="opening_balance",
            idempotency_key="opening-test-balance",
            details={"historical": True},
        )
        session.add_all([operation, sub, ledger])
        await session.commit()
        return SimpleNamespace(
            actor=actor.id,
            user=user.id,
            server=server.id,
            config=cfg.id,
            name=cfg.name,
            operation=operation.operation_id,
            subscription=sub.id,
            ledger=ledger.id,
            payload=payload,
        )


async def apply(service, approved, state, *, batch="retire-test"):
    return await service.apply(
        approved, batch_id=batch, actor_user_id=state.actor, reason=REASON
    )


async def assert_unchanged(sessionmaker, state):
    async with sessionmaker() as session:
        assert await session.get(VPN_Config, state.config) is not None
        assert (await session.get(Server, state.server)).lifecycle_state == "active"
        assert (await session.get(User, state.user)).balance == Decimal("99.93")
        assert not (await session.scalars(select(AdminAuditEvent))).all()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,plan", [("suspended", "basic"), ("expired", "plus"), ("cancelled", "basic")]
)
async def test_archive_preserves_paused_entitlement_finances_and_history(
    sessionmaker, status, plan
):
    state = await seed_legacy(sessionmaker, status=status, plan=plan)
    async with sessionmaker() as session, session.begin():
        user = await session.get(User, state.user)
        user.unlimited_balance = True
        user.exclude_from_business_metrics = True
        node = server_row(node=True)
        session.add(node)
        await session.flush()
        modern = VPN_Config(
            name="independent-modern-key",
            display_name="NL key",
            owner_id=state.user,
            server_id=node.id,
            service_id="hysteria",
            access_policy={"max_sessions": 1},
            desired_state="suspended",
            actual_state="suspended",
            suspended=True,
        )
        session.add(modern)
        old_ops = []
        for op_status in ("pending", "failed", "rejected", "exhausted", "superseded"):
            op = VPNOperation(
                operation_id=str(uuid.uuid4()),
                config_id=state.config,
                config_name=state.name,
                server_id=state.server,
                owner_id=state.user,
                kind="revoke",
                status=op_status,
                attempts=2,
                payload={"original_status": op_status},
            )
            old_ops.append((op.operation_id, op_status, copy.deepcopy(op.payload)))
            session.add(op)
        await session.flush()
        modern_id, node_id = modern.id, node.id
    service = OpenVPNRetirement(uow)
    manifest = await service.preview()
    assert "not-a-real-manager-token" not in repr(manifest)
    result = await apply(service, manifest, state)
    assert result == {
        "archived_configs": 1,
        "accounts_detached": 1,
        "servers_retired": [state.server],
        "replayed": False,
    }
    async with sessionmaker() as session:
        assert await session.get(VPN_Config, state.config) is None
        cfg = await session.get(VPN_Config, modern_id)
        assert cfg.server_id == node_id and cfg.actual_state == "suspended"
        assert cfg.access_policy == {"max_sessions": 1}
        assert (await session.get(Server, node_id)).lifecycle_state == "active"
        server = await session.get(Server, state.server)
        assert server.lifecycle_state == "retired" and not server.accepts_new_configs
        user = await session.get(User, state.user)
        assert (
            user.balance,
            user.unlimited_balance,
            user.exclude_from_business_metrics,
        ) == (Decimal("99.93"), True, True)
        ledger = (await session.scalars(select(LedgerEntry))).all()
        assert len(ledger) == 1 and ledger[0].id == state.ledger
        assert ledger[0].amount == ledger[0].balance_after == Decimal("99.93")
        assert ledger[0].details == {"historical": True}
        sub = await session.get(Subscription, state.subscription)
        assert (sub.status, sub.plan_code, sub.version) == (status, plan, 4)
        assert sub.migration_snapshot["legacy_config_ids"] == [state.config]
        assert sub.migration_snapshot["historic_extra"] == "keep"
        assert sub.migration_snapshot["cutover_state"] == "complete"
        assert (
            sub.migration_snapshot["openvpn_decommission"]["remote_revocation_verified"]
            is False
        )
        for op_id, previous_status, original_payload in old_ops:
            op = await session.scalar(
                select(VPNOperation).where(VPNOperation.operation_id == op_id)
            )
            expected = (
                "superseded"
                if previous_status in {"pending", "failed"}
                else previous_status
            )
            assert op.status == expected and op.payload == original_payload
            assert op.config_id is None
        old = await session.scalar(
            select(VPNOperation).where(VPNOperation.operation_id == state.operation)
        )
        assert old.status == "succeeded" and old.kind == "suspend"
        assert old.config_id is None and old.payload == state.payload
        events = (await session.scalars(select(AdminAuditEvent))).all()
        archives = [
            event for event in events if event.action == "openvpn.credential_archived"
        ]
        assert len(archives) == 1
        archived = archives[0].details
        assert archived["config"]["id"] == state.config
        assert archived["config"]["name"] == state.name
        assert archived["remote_revocation_verified"] is False
        assert archived["shutdown_attested_by_owner"] is True
        assert state.operation in archived["operation_ids"]
        assert len([event for event in events if event.action == ACTION]) == 1


@pytest.mark.asyncio
async def test_basic_can_resume_on_nl_and_retirement_replay_cannot_pause_it(
    sessionmaker, monkeypatch
):
    node, user, server, configs = await setup_node(monkeypatch)
    state = await seed_legacy(sessionmaker, user_id=user.id)
    monkeypatch.setattr(settings, "billing_enabled", True)

    async def catalog():
        return (
            SimpleNamespace(
                id="hysteria",
                capabilities={
                    "max_sessions": True,
                    "session_scope": "auth_id_per_server_instance",
                    "enforcement": "atomic_quic_connection",
                },
            ),
        )

    def manager_must_not_be_contacted(*args, **kwargs):
        pytest.fail("Retired Manager must never be contacted")

    node.catalog = catalog
    monkeypatch.setattr(configs, "_create_gateway", manager_must_not_be_contacted)
    subscriptions = SubscriptionService(uow, config_service=configs)
    with pytest.raises(InvalidOperationError, match="Pause billing"):
        await subscriptions.resume(state.user)
    service = OpenVPNRetirement(uow)
    manifest = await service.preview()
    await apply(service, manifest, state)
    assert (await subscriptions.get_current(state.user)).status == "suspended"
    granted = await subscriptions.resume(state.user, request_key="customer-resume")
    assert granted.status == "active" and granted.plan_code == "basic"
    assert len(granted.accesses) == 1
    access = granted.accesses[0]
    assert access.service_id == "hysteria" and access.status == "active"
    assert node.accesses[access.remote_access_id]["policy"]["max_sessions"] == 1
    async with sessionmaker() as session:
        before = await session.get(Subscription, state.subscription)
        before_version = before.version
        audit_count = len((await session.scalars(select(AdminAuditEvent))).all())
    replay = await apply(service, manifest, state)
    assert replay["replayed"] is True
    after = await subscriptions.get_current(state.user)
    assert after.status == "active" and after.version == before_version
    assert after.accesses == granted.accesses and len(node.creates) == 1
    async with sessionmaker() as session:
        assert (
            len((await session.scalars(select(AdminAuditEvent))).all()) == audit_count
        )
        assert (await session.get(User, state.user)).balance == Decimal("99.93")
        assert len((await session.scalars(select(LedgerEntry))).all()) == 1


@pytest.mark.asyncio
async def test_stale_approved_snapshot_rolls_back_without_archival(sessionmaker):
    state = await seed_legacy(sessionmaker)
    service = OpenVPNRetirement(uow)
    approved = await service.preview()
    async with sessionmaker() as session, session.begin():
        (await session.get(VPN_Config, state.config)).display_name = (
            "Changed after review"
        )
    with pytest.raises(InvalidOperationError, match="snapshot changed"):
        await apply(service, approved, state)
    await assert_unchanged(sessionmaker, state)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe",
    [
        "running",
        "actual_active",
        "desired_active",
        "active_subscription",
        "missing_subscription",
    ],
)
async def test_unsafe_current_state_is_rejected_atomically(sessionmaker, unsafe):
    state = await seed_legacy(sessionmaker)
    async with sessionmaker() as session, session.begin():
        cfg = await session.get(VPN_Config, state.config)
        if unsafe == "running":
            op = await session.scalar(
                select(VPNOperation).where(VPNOperation.operation_id == state.operation)
            )
            op.status, op.lease_token, op.lease_until = (
                "running",
                str(uuid.uuid4()),
                datetime.now(timezone.utc),
            )
        elif unsafe == "actual_active":
            cfg.actual_state = "active"
        elif unsafe == "desired_active":
            cfg.desired_state = "active"
        elif unsafe == "active_subscription":
            (await session.get(Subscription, state.subscription)).status = "active"
        else:
            await session.delete(await session.get(Subscription, state.subscription))
    service = OpenVPNRetirement(uow)
    with pytest.raises(InvalidOperationError):
        await apply(service, await service.preview(), state)
    await assert_unchanged(sessionmaker, state)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "op_status,referenced",
    [("pending", False), ("exhausted", False), ("succeeded", True), ("rejected", True)],
)
async def test_surviving_node_dependency_fails_closed_even_for_terminal_op(
    sessionmaker, op_status, referenced
):
    state = await seed_legacy(sessionmaker)
    async with sessionmaker() as session, session.begin():
        node = server_row(node=True)
        session.add(node)
        await session.flush()
        op_id = str(uuid.uuid4())
        cfg = VPN_Config(
            name="staged-node-key",
            display_name="NL",
            owner_id=state.user,
            server_id=node.id,
            service_id="hysteria",
            operation_id=op_id if referenced else None,
            desired_state="active",
            actual_state="provisioning",
        )
        session.add(cfg)
        await session.flush()
        session.add(
            VPNOperation(
                operation_id=op_id,
                config_id=cfg.id,
                config_name=cfg.name,
                server_id=node.id,
                owner_id=state.user,
                kind="provision",
                status=op_status,
                payload={
                    "subscription_prior_configs": [
                        {
                            "id": state.config,
                            "owner_id": state.user,
                            "backend_type": "openvpn-manager",
                            "server_id": state.server,
                            "config_name": state.name,
                        }
                    ]
                },
            )
        )
    service = OpenVPNRetirement(uow)
    manifest = await service.preview()
    assert manifest["node_dependency_blockers"] == [op_id]
    with pytest.raises(InvalidOperationError, match="dependencies"):
        await apply(service, manifest, state)
    await assert_unchanged(sessionmaker, state)


@pytest.mark.asyncio
@pytest.mark.parametrize("link", ["access", "binding"])
async def test_legacy_config_linked_as_modern_access_requires_separate_review(
    sessionmaker, link
):
    state = await seed_legacy(sessionmaker)
    async with sessionmaker() as session, session.begin():
        if link == "access":
            session.add(
                SubscriptionAccess(
                    subscription_id=state.subscription,
                    service_id="hysteria",
                    config_id=state.config,
                    generation=1,
                )
            )
        else:
            session.add(
                VPNAccessBinding(
                    config_id=state.config,
                    config_ref=state.config,
                    server_id=state.server,
                    node_id="nl-1",
                    service_id="hysteria",
                    provision_operation_id=state.operation,
                    policy={"max_sessions": 1},
                )
            )
    service = OpenVPNRetirement(uow)
    with pytest.raises(InvalidOperationError, match="dependencies"):
        await apply(service, await service.preview(), state)
    await assert_unchanged(sessionmaker, state)


@pytest.mark.asyncio
async def test_retirement_detaches_all_legacy_owners_without_resuming_them(
    sessionmaker,
):
    state = await seed_legacy(sessionmaker)
    async with sessionmaker() as session, session.begin():
        second = User(tg_id=987654, balance=Decimal("12.34"))
        session.add(second)
        await session.flush()
        cfg = VPN_Config(
            name="second-old-identity",
            display_name="Another old phone",
            server_id=state.server,
            owner_id=second.id,
            suspended=True,
            desired_state="suspended",
            actual_state="suspended",
        )
        session.add(cfg)
        await session.flush()
        sub = Subscription(
            user_id=second.id,
            plan_code="plus",
            status="cancelled",
            version=8,
            migration_snapshot={
                "legacy_config_ids": [cfg.id],
                "cutover_state": "pending",
            },
        )
        session.add(sub)
        await session.flush()
        second_id, second_sub = second.id, sub.id
    service = OpenVPNRetirement(uow)
    result = await apply(service, await service.preview(), state)
    assert result["archived_configs"] == result["accounts_detached"] == 2
    async with sessionmaker() as session:
        assert not (await session.scalars(select(VPN_Config))).all()
        assert (
            await session.get(Subscription, state.subscription)
        ).status == "suspended"
        sub = await session.get(Subscription, second_sub)
        assert (sub.status, sub.plan_code, sub.version) == ("cancelled", "plus", 9)
        assert sub.migration_snapshot["cutover_state"] == "complete"
        assert (await session.get(User, second_id)).balance == Decimal("12.34")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revoke_status,matching_name,allowed",
    [
        (None, False, False),
        ("failed", True, False),
        ("succeeded", False, False),
        ("succeeded", True, True),
    ],
)
async def test_orphan_legacy_dependency_requires_existing_exact_revoke_proof(
    sessionmaker, revoke_status, matching_name, allowed
):
    state = await seed_legacy(sessionmaker)
    async with sessionmaker() as session, session.begin():
        node = server_row(node=True)
        session.add(node)
        await session.flush()
        op_id = str(uuid.uuid4())
        session.add(
            VPNOperation(
                operation_id=op_id,
                config_id=None,
                config_name="other-node-intent",
                server_id=node.id,
                owner_id=state.user,
                kind="provision",
                status="pending",
                payload={
                    "subscription_prior_configs": [
                        {
                            "id": state.config + 10000,
                            "owner_id": state.user,
                            "backend_type": "openvpn-manager",
                            "server_id": state.server,
                            "config_name": "previously-removed-identity",
                        }
                    ]
                },
            )
        )
        if revoke_status:
            session.add(
                VPNOperation(
                    operation_id=str(uuid.uuid4()),
                    config_id=None,
                    config_name=(
                        "previously-removed-identity"
                        if matching_name
                        else "different-identity"
                    ),
                    server_id=state.server,
                    owner_id=state.user,
                    kind="revoke",
                    status=revoke_status,
                    payload={"preexisting": True},
                )
            )
    service = OpenVPNRetirement(uow)
    manifest = await service.preview()
    assert manifest["node_dependency_blockers"] == ([] if allowed else [op_id])
    if allowed:
        await apply(service, manifest, state)
        async with sessionmaker() as session:
            operation = await session.scalar(
                select(VPNOperation).where(VPNOperation.operation_id == op_id)
            )
            assert operation.status == "pending"
            assert (
                operation.payload["subscription_prior_configs"][0]["config_name"]
                == "previously-removed-identity"
            )
    else:
        with pytest.raises(InvalidOperationError, match="dependencies"):
            await apply(service, manifest, state)
        await assert_unchanged(sessionmaker, state)


@pytest.mark.asyncio
async def test_terminal_node_dependency_referenced_only_by_access_still_blocks(
    sessionmaker,
):
    state = await seed_legacy(sessionmaker)
    async with sessionmaker() as session, session.begin():
        node = server_row(node=True)
        session.add(node)
        await session.flush()
        cfg = VPN_Config(
            name="node-with-access-pointer",
            display_name="NL",
            server_id=node.id,
            owner_id=state.user,
            service_id="hysteria",
            actual_state="suspended",
            desired_state="suspended",
            suspended=True,
        )
        session.add(cfg)
        await session.flush()
        op_id = str(uuid.uuid4())
        session.add_all(
            [
                SubscriptionAccess(
                    subscription_id=state.subscription,
                    config_id=cfg.id,
                    service_id="hysteria",
                    provision_operation_id=op_id,
                ),
                VPNOperation(
                    operation_id=op_id,
                    config_id=cfg.id,
                    config_name=cfg.name,
                    server_id=node.id,
                    owner_id=state.user,
                    kind="provision",
                    status="succeeded",
                    payload={
                        "subscription_prior_configs": [
                            {
                                "id": state.config,
                                "owner_id": state.user,
                                "backend_type": "openvpn-manager",
                                "server_id": state.server,
                                "config_name": state.name,
                            }
                        ]
                    },
                ),
            ]
        )
    service = OpenVPNRetirement(uow)
    manifest = await service.preview()
    assert manifest["node_dependency_blockers"] == [op_id]
    with pytest.raises(InvalidOperationError, match="dependencies"):
        await apply(service, manifest, state)
    await assert_unchanged(sessionmaker, state)


@pytest.mark.asyncio
async def test_binding_with_mismatched_historical_reference_still_blocks(sessionmaker):
    state = await seed_legacy(sessionmaker)
    async with sessionmaker() as session, session.begin():
        session.add(
            VPNAccessBinding(
                config_id=state.config,
                config_ref=state.config + 10000,
                server_id=state.server,
                node_id="nl-1",
                service_id="hysteria",
                provision_operation_id=state.operation,
                policy={},
            )
        )
    service = OpenVPNRetirement(uow)
    manifest = await service.preview()
    assert manifest["legacy_binding_links"]
    with pytest.raises(InvalidOperationError, match="dependencies"):
        await apply(service, manifest, state)
    await assert_unchanged(sessionmaker, state)
