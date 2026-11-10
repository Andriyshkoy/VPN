from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from core.config import settings
from core.db.models import LedgerEntry, VPNOperation
from core.db.unit_of_work import uow
from core.exceptions import APITransportError, InvalidOperationError
from core.services.subscription_migration import SubscriptionMigrationService
from core.services.subscriptions import (
    SubscriptionService,
    SubscriptionUnavailableError,
)
from tests.test_node_lifecycle import setup_node


async def product(monkeypatch, *, capable=True):
    node, user, server, configs = await setup_node(monkeypatch)
    monkeypatch.setattr(settings, "billing_enabled", False)

    async def catalog():
        return (
            SimpleNamespace(
                id="hysteria",
                capabilities={
                    "max_sessions": capable,
                    "session_scope": "auth_id_per_server_instance",
                    "enforcement": "atomic_quic_connection",
                },
            ),
        )

    node.catalog = catalog
    return node, user, server, SubscriptionService(uow, config_service=configs)


@pytest.mark.asyncio
async def test_two_protocols_are_free_entitlements_of_one_account(
    sessionmaker, monkeypatch
):
    node, user, server, service = await product(monkeypatch)
    selected = await service.select_plan(user.id, "plus")
    assert selected.status == "pending" and selected.billing_paused
    assert str(selected.hourly_rate_rub) == "0.14"
    assert not hasattr(selected, "current_period_end")
    granted = await service.issue(user.id, server.id, request_key="initial")
    assert granted.status == "active"
    assert len(granted.accesses) == 2
    assert all(item.status == "active" for item in granted.accesses)
    assert len(node.creates) == 2
    assert (await service.issue(user.id, server.id)).accesses == granted.accesses
    assert len(node.creates) == 2
    profile = await service.export(user.id, "hysteria")
    assert profile.uri and profile.uri not in repr(profile)
    async with uow() as repos:
        assert (await repos["users"].get(id=user.id)).balance == user.balance
        assert not (
            await repos["users"].session.scalars(
                select(LedgerEntry).where(LedgerEntry.user_id == user.id)
            )
        ).all()


@pytest.mark.asyncio
async def test_basic_fails_closed_without_engine_capability(sessionmaker, monkeypatch):
    node, user, server, service = await product(monkeypatch, capable=False)
    await service.select_plan(user.id, "basic")
    with pytest.raises(SubscriptionUnavailableError) as exc:
        await service.issue(user.id, server.id)
    assert exc.value.reason_code == "session_limit_unavailable"
    assert not node.creates
    assert (await service.get_current(user.id)).status == "pending"


@pytest.mark.asyncio
async def test_downgrade_revokes_both_old_credentials_before_limited_hys(
    sessionmaker, monkeypatch
):
    node, user, server, service = await product(monkeypatch)
    await service.select_plan(user.id, "plus")
    previous = await service.issue(user.id, server.id)
    old_ids = {item.remote_access_id for item in previous.accesses}
    await service.select_plan(user.id, "basic", expected_version=previous.version)
    with pytest.raises(InvalidOperationError):
        await service.export(user.id, "vless-reality")
    node.calls.clear()
    result = await service.issue(user.id, server.id)
    assert [call[0] for call in node.calls] == ["revoked", "revoked", "create"]
    assert all(node.accesses[key]["status"] == "revoked" for key in old_ids)
    hys = next(item for item in result.accesses if item.service_id == "hysteria")
    assert node.accesses[hys.remote_access_id]["policy"]["max_sessions"] == 1
    assert hys.remote_access_id not in old_ids


@pytest.mark.asyncio
async def test_failed_revoke_blocks_replacement_even_in_reconciler(
    sessionmaker, monkeypatch
):
    node, user, server, service = await product(monkeypatch)
    await service.select_plan(user.id, "plus")
    await service.issue(user.id, server.id)
    await service.select_plan(user.id, "basic")

    async def unavailable(*args, **kwargs):
        raise APITransportError("offline")

    node.revoke_access = unavailable
    with pytest.raises(APITransportError):
        await service.issue(user.id, server.id)
    async with uow() as repos:
        operations = (
            await repos["users"].session.scalars(
                select(VPNOperation).where(VPNOperation.kind == "provision")
            )
        ).all()
        replacement = next(
            op for op in operations if op.payload.get("subscription_prior_configs")
        )
        operation_id = replacement.operation_id
    assert (
        await service._config_service._execute(operation_id)
        == "deferred:subscription_revocation_pending"
    )
    assert len(node.creates) == 2


@pytest.mark.asyncio
async def test_reissue_request_is_idempotent_and_no_wallet_charge(
    sessionmaker, monkeypatch
):
    node, user, server, service = await product(monkeypatch)
    await service.select_plan(user.id, "basic")
    await service.issue(user.id, server.id)
    first = await service.regenerate(user.id, "hysteria", "same-request")
    second = await service.regenerate(user.id, "hysteria", "same-request")
    assert first.accesses == second.accesses
    assert len(node.creates) == 2
    assert first.accesses[0].generation == 2


@pytest.mark.asyncio
async def test_reconciler_may_finish_plan_revocation_before_issue(
    sessionmaker, monkeypatch
):
    node, user, server, service = await product(monkeypatch)
    await service.select_plan(user.id, "plus")
    await service.issue(user.id, server.id)
    await service.select_plan(user.id, "basic")
    await service._config_service.reconcile()
    current = await service.issue(user.id, server.id)
    hys = next(item for item in current.accesses if item.service_id == "hysteria")
    assert hys.status == "active" and hys.generation == 2
    assert node.accesses[hys.remote_access_id]["policy"]["max_sessions"] == 1


@pytest.mark.asyncio
async def test_migration_preview_stale_guard_and_idempotent_no_money_changes(
    sessionmaker, monkeypatch
):
    node, user, server, service = await product(monkeypatch)
    configs = service._config_service
    cfg = await configs.create_config(
        server_id=server.id,
        owner_id=user.id,
        name="old-node-key",
        display_name="Old",
        service_id="hysteria",
        access_policy={},
    )
    migration = SubscriptionMigrationService(uow)
    manifest = await migration.preview()
    assert manifest[0].active_config_count == 1 and manifest[0].plan_code == "basic"
    async with uow() as repos:
        await repos["configs"].set_desired_state(cfg.id, desired_state="suspended")
    with pytest.raises(InvalidOperationError, match="snapshot changed"):
        await migration.apply(manifest, source="reviewed-test")
    revised = await migration.preview()
    assert revised[0].status == "suspended"
    ids = await migration.apply(revised, source="reviewed-test")
    assert await migration.apply(revised, source="reviewed-test") == ids
    current = await service.get_current(user.id)
    assert current.status == "suspended" and not current.unlimited_balance
    assert current.accesses == ()
    assert len(node.creates) == 1
    async with uow() as repos:
        user_row = await repos["users"].get(id=user.id)
        assert user_row.balance == user.balance and not user_row.unlimited_balance
        assert await repos["configs"].get(id=cfg.id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "active_count,plan,status",
    [(2, "plus", "active"), (1, "basic", "active"), (0, "basic", "suspended")],
)
async def test_migration_counts_active_configs_only(
    sessionmaker, monkeypatch, active_count, plan, status
):
    _, user, server, service = await product(monkeypatch)
    for index, protocol in enumerate(("hysteria", "vless-reality")):
        cfg = await service._config_service.create_config(
            server_id=server.id,
            owner_id=user.id,
            name=f"old-{index}",
            display_name="Existing",
            service_id=protocol,
            access_policy={},
        )
        if index >= active_count:
            await service._config_service.suspend_config(cfg.id)
    migration = SubscriptionMigrationService(uow)
    preview = await migration.preview()
    assert preview[0].active_config_count == active_count
    assert preview[0].plan_code == plan and preview[0].status == status
    await migration.apply(preview, source="active-count-test")
    current = await service.get_current(user.id)
    assert current.plan_code == plan
    assert current.status == ("pending" if status == "active" else status)


@pytest.mark.parametrize("invalid", [True, False, 0, -1, 129, "1", 1.0])
def test_session_policy_strict_and_preserves_legacy_snapshot(invalid):
    from core.services.node_policy import normalize_node_policy

    assert "max_sessions" not in normalize_node_policy("hysteria", {})
    assert (
        normalize_node_policy("hysteria", {"max_sessions": None})["max_sessions"]
        is None
    )
    with pytest.raises(InvalidOperationError):
        normalize_node_policy("hysteria", {"max_sessions": invalid})
    with pytest.raises(InvalidOperationError):
        normalize_node_policy("vless-reality", {"max_sessions": 1})


@pytest.mark.asyncio
async def test_failed_resume_keeps_suspended_account_unbillable(
    sessionmaker, monkeypatch
):
    node, user, server, service = await product(monkeypatch)
    await service.select_plan(user.id, "basic")
    await service.issue(user.id, server.id)
    paused = await service.suspend(user.id)

    async def unsupported():
        return (SimpleNamespace(id="hysteria", capabilities={"max_sessions": False}),)

    node.catalog = unsupported
    with pytest.raises(SubscriptionUnavailableError):
        await service.resume(user.id)
    after = await service.get_current(user.id)
    assert after.status == "suspended" and after.version == paused.version
    now = datetime.now(timezone.utc)
    async with uow() as repos:
        assert (
            await repos["billing"].charge_period(
                period_key="failed-resume",
                period_start=now,
                period_end=now + timedelta(hours=1),
                cost_per_config="0.07",
            )
            == []
        )


@pytest.mark.asyncio
async def test_ambiguous_first_grant_stays_unbillable_and_retry_finalizes(
    sessionmaker, monkeypatch
):
    node, user, server, service = await product(monkeypatch)
    await service.select_plan(user.id, "plus")
    node.timeout_after_create = True
    with pytest.raises(APITransportError):
        await service.issue(user.id, server.id)
    assert (await service.get_current(user.id)).status == "pending"
    now = datetime.now(timezone.utc)
    async with uow() as repos:
        assert (
            await repos["billing"].charge_period(
                period_key="failed-first-grant",
                period_start=now,
                period_end=now + timedelta(hours=1),
                cost_per_config="0.07",
            )
            == []
        )
    service._config_service._clock = lambda: now + timedelta(minutes=5)
    await service._config_service.reconcile()
    after = await service.get_current(user.id)
    assert after.status == "active" and len(after.accesses) == 2
    assert all(item.status == "active" for item in after.accesses)
    assert len(node.creates) == 2


@pytest.mark.asyncio
async def test_migration_pause_covers_old_access_and_cutover_retires_it(
    sessionmaker, monkeypatch
):
    node, user, server, service = await product(monkeypatch)
    old = await service._config_service.create_config(
        server_id=server.id,
        owner_id=user.id,
        name="legacy-unlimited",
        display_name="Old unlimited",
        service_id="hysteria",
        access_policy={},
    )
    migration = SubscriptionMigrationService(uow)
    await migration.apply(await migration.preview(), source="cutover-test")
    assert (await service.get_current(user.id)).status == "pending"
    await service.suspend(user.id)
    assert (await service._config_service.get(old.id)).actual_state == "suspended"
    node.calls.clear()
    after = await service.resume(user.id)
    assert after.status == "active"
    assert [item[0] for item in node.calls] == ["revoked", "create"]
    assert await service._config_service.get(old.id) is None
    async with uow() as repos:
        assert (await repos["subscriptions"].get_by_user(user.id)).migration_snapshot[
            "cutover_state"
        ] == "complete"


@pytest.mark.asyncio
async def test_direct_plan_selection_captures_existing_legacy_cutover(
    sessionmaker, monkeypatch
):
    node, user, server, service = await product(monkeypatch)
    old = await service._config_service.create_config(
        server_id=server.id,
        owner_id=user.id,
        name="existing-outside-product",
        display_name="Existing",
        service_id="hysteria",
        access_policy={},
    )
    await service.select_plan(user.id, "basic")
    now = datetime.now(timezone.utc)
    async with uow() as repos:
        with pytest.raises(InvalidOperationError, match="cutover"):
            await repos["billing"].charge_period(
                period_key="unsafe-cutover",
                period_start=now,
                period_end=now + timedelta(hours=1),
                cost_per_config="0.07",
            )
    await service.issue(user.id, server.id)
    assert await service._config_service.get(old.id) is None
    assert (await service.get_current(user.id)).status == "active"


@pytest.mark.asyncio
async def test_account_deletion_retains_product_and_campaign_history(
    sessionmaker, monkeypatch
):
    from uuid import uuid4

    from core.db.models.campaign import AdminCampaign, CampaignRecipient
    from core.services import UserService

    _, user, _, service = await product(monkeypatch)
    await service.select_plan(user.id, "basic")
    assert await UserService(uow).delete(user.id) is False
    recipient = await UserService(uow).register(9112347)
    async with uow() as repos:
        campaign = AdminCampaign(
            public_id=str(uuid4()), name="History", text="Fixture", audience={}
        )
        repos["users"].session.add(campaign)
        await repos["users"].session.flush()
        repos["users"].session.add(
            CampaignRecipient(
                campaign_id=campaign.id,
                user_id=recipient.id,
                tg_id_snapshot=recipient.tg_id,
                outbox_dedupe_key="history:fixture",
            )
        )
    assert await UserService(uow).delete(recipient.id) is False


@pytest.mark.asyncio
async def test_wallet_adjustments_respect_manual_pause_and_unlimited_mode(
    sessionmaker, monkeypatch
):
    from core.services import UserService

    _, user, server, service = await product(monkeypatch)
    await service.select_plan(user.id, "basic")
    await service.issue(user.id, server.id)
    await service.suspend(user.id)
    await UserService(uow).update(user.id, balance="5.00")
    assert (await service.get_current(user.id)).status == "suspended"
    await service.resume(user.id)
    async with uow() as repos:
        await repos["users"].update(user.id, unlimited_balance=True)
    await UserService(uow).update(user.id, balance="0.00")
    view = await service.get_current(user.id)
    assert view.status == "active" and view.unlimited_balance
    assert all(item.status == "active" for item in view.accesses)


@pytest.mark.asyncio
async def test_resume_after_failed_remote_suspend_confirms_activation(
    sessionmaker, monkeypatch
):
    node, user, server, service = await product(monkeypatch)
    await service.select_plan(user.id, "basic")
    await service.issue(user.id, server.id)
    original = node.suspend_access

    async def offline(*args, **kwargs):
        raise APITransportError("offline")

    node.suspend_access = offline
    paused = await service.suspend(user.id)
    assert paused.status == "suspended"
    assert paused.accesses[0].status == "suspended"
    assert next(iter(node.accesses.values()))["status"] == "active"
    node.suspend_access = original
    after = await service.resume(user.id)
    assert after.status == "active" and after.accesses[0].status == "active"


@pytest.mark.asyncio
async def test_new_legacy_cutover_cannot_interrupt_enabled_billing(
    sessionmaker, monkeypatch
):
    _, user, server, service = await product(monkeypatch)
    await service._config_service.create_config(
        server_id=server.id,
        owner_id=user.id,
        name="not-yet-migrated",
        display_name="Existing",
        service_id="hysteria",
        access_policy={},
    )
    monkeypatch.setattr(settings, "billing_enabled", True)
    with pytest.raises(InvalidOperationError, match="Pause billing"):
        await service.select_plan(user.id, "basic")
    assert await service.get_current(user.id) is None


@pytest.mark.asyncio
async def test_topup_does_not_rollback_money_when_mapped_credential_failed(
    sessionmaker, monkeypatch
):
    from core.services import BillingService, UserService

    _, user, server, service = await product(monkeypatch)
    await service.select_plan(user.id, "basic")
    granted = await service.issue(user.id, server.id)
    async with uow() as repos:
        row = await repos["subscriptions"].get_by_user(user.id)
        row.status = "expired"
        cfg = await repos["configs"].get(id=granted.accesses[0].config_id)
        cfg.actual_state, cfg.desired_state = "failed", "suspended"
    await BillingService(uow, per_config_cost="0.07").top_up(
        user.id, "2.00", idempotency_key="failed-credential-topup"
    )
    assert str((await UserService(uow).get(user.id)).balance) == "2.00"
    assert (await service.get_current(user.id)).status == "pending"
    await service.suspend(user.id)
    assert (await service.get_current(user.id)).status == "suspended"


@pytest.mark.asyncio
async def test_suspended_legacy_resume_cannot_interrupt_enabled_billing(
    sessionmaker, monkeypatch
):
    _, user, server, service = await product(monkeypatch)
    old = await service._config_service.create_config(
        server_id=server.id,
        owner_id=user.id,
        name="legacy-paused",
        display_name="Existing",
        service_id="hysteria",
        access_policy={},
    )
    await service._config_service.suspend_config(old.id)
    migration = SubscriptionMigrationService(uow)
    await migration.apply(await migration.preview(), source="paused-cutover")
    monkeypatch.setattr(settings, "billing_enabled", True)
    with pytest.raises(InvalidOperationError, match="Pause billing"):
        await service.resume(user.id)
    assert (await service.get_current(user.id)).status == "suspended"
