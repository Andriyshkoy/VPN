from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from core.config import Settings, settings
from core.db.models.config import VPN_Config
from core.db.models.ledger import LedgerEntry, LedgerKind
from core.db.models.payment import ProviderPayment
from core.db.models.referral_reward import ReferralReward
from core.db.models.server import Server
from core.db.models.user import User
from core.db.repo.billing import BillingRepo
from core.db.unit_of_work import uow
from core.exceptions import InvalidOperationError
from core.services import BillingService, UserService


async def _referral_chain(*tg_ids: int) -> list[int]:
    """Create a root-to-leaf referral chain and return internal user IDs."""

    user_ids: list[int] = []
    async with uow() as repos:
        parent_id = None
        for tg_id in tg_ids:
            user = await repos["users"].add(
                User(tg_id=tg_id, balance=Decimal("0.00"), referred_by_id=parent_id)
            )
            user_ids.append(user.id)
            parent_id = user.id
    return user_ids


async def _capture(billing: BillingService, *, user_id: int, amount: str, key: str):
    intent = await billing.create_payment_intent(
        user_id=user_id,
        amount=amount,
        currency="RUB",
    )
    return await billing.record_provider_payment(
        user_id=user_id,
        provider="telegram",
        provider_payment_id=key,
        amount=amount,
        currency="RUB",
        payload=intent.payload,
        intent_id=intent.intent_id,
    )


@pytest.mark.parametrize(
    ("program_version", "level_1_rate_bps", "level_2_rate_bps"),
    (
        ("v1-5pct-1pct", 400, 100),
        ("v2-6pct-1pct", 600, 100),
    ),
)
def test_referral_policy_cannot_change_in_place(
    program_version,
    level_1_rate_bps,
    level_2_rate_bps,
):
    with pytest.raises(ValueError, match="Unsupported referral program policy"):
        Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            encryption_key=settings.encryption_key,
            referral_program_version=program_version,
            referral_level_1_rate_bps=level_1_rate_bps,
            referral_level_2_rate_bps=level_2_rate_bps,
        )


@pytest.mark.asyncio
async def test_provider_payment_credits_two_referral_levels_once(sessionmaker):
    root_id, direct_id, payer_id = await _referral_chain(8101, 8102, 8103)
    billing = BillingService(uow, per_config_cost="1.00")

    first = await _capture(
        billing,
        user_id=payer_id,
        amount="500.00",
        key="referral-payment-500",
    )
    replay = await billing.record_provider_payment(
        user_id=payer_id,
        provider="telegram",
        provider_payment_id="referral-payment-500",
        amount="500.00",
        currency="RUB",
        payload=f"topup:{first.provider_payment_id}",
    )

    # The replay above deliberately uses a non-intent legacy payload; existing
    # captured charges validate identity/amount and still cannot mint rewards.
    assert first.credited is True
    assert replay.credited is False
    assert (await UserService(uow).get(payer_id)).balance == Decimal("500.00")
    assert (await UserService(uow).get(direct_id)).balance == Decimal("25.00")
    assert (await UserService(uow).get(root_id)).balance == Decimal("5.00")

    async with uow() as repos:
        rewards = (
            await repos["users"].session.scalars(
                select(ReferralReward).order_by(ReferralReward.level)
            )
        ).all()
        payment = await repos["users"].session.scalar(select(ProviderPayment))
        direct_ledger = await repos["billing"].list_ledger_entries(direct_id)
        root_ledger = await repos["billing"].list_ledger_entries(root_id)

    assert [(row.level, row.rate_bps, row.reward_amount) for row in rewards] == [
        (1, 500, Decimal("25.00")),
        (2, 100, Decimal("5.00")),
    ]
    assert rewards[0].source_user_id == payer_id
    assert rewards[0].beneficiary_user_id == direct_id
    assert rewards[0].program_version == "v1-5pct-1pct"
    assert payment.referral_settlement_status == "rewarded"
    assert payment.referral_program_version == "v1-5pct-1pct"
    assert payment.referral_settled_at is not None
    assert direct_ledger[0].kind == LedgerKind.REFERRAL_REWARD_L1.value
    assert direct_ledger[0].details["retroactive"] is False
    assert root_ledger[0].kind == LedgerKind.REFERRAL_REWARD_L2.value


@pytest.mark.asyncio
async def test_rewards_never_compound_or_extend_beyond_two_levels(sessionmaker):
    root_id, level_three_id, level_two_id, payer_id = await _referral_chain(
        8201, 8202, 8203, 8204
    )
    billing = BillingService(uow, per_config_cost="1.00")

    await billing.top_up(
        payer_id,
        "100.00",
        idempotency_key="manual-top-up-does-not-reward",
    )
    await UserService(uow).update(payer_id, balance="150.00")
    opening_user = await UserService(uow).register(
        8205,
        referred_by_id=level_two_id,
        balance="50.00",
    )
    assert (await UserService(uow).get(level_two_id)).balance == Decimal("0.00")
    assert opening_user.balance == Decimal("50.00")
    async with uow() as repos:
        assert (
            await repos["users"].session.scalars(select(ReferralReward))
        ).all() == []

    await _capture(
        billing,
        user_id=payer_id,
        amount="100.00",
        key="two-level-boundary",
    )

    assert (await UserService(uow).get(level_two_id)).balance == Decimal("5.00")
    assert (await UserService(uow).get(level_three_id)).balance == Decimal("1.00")
    assert (await UserService(uow).get(root_id)).balance == Decimal("0.00")
    async with uow() as repos:
        rewards = (
            await repos["users"].session.scalars(
                select(ReferralReward).order_by(ReferralReward.level)
            )
        ).all()
    assert len(rewards) == 2


@pytest.mark.asyncio
async def test_referral_reward_rounds_half_up_and_skips_zero_rows(sessionmaker):
    root_id, direct_id, payer_id = await _referral_chain(8301, 8302, 8303)
    billing = BillingService(uow, per_config_cost="1.00")

    await _capture(
        billing,
        user_id=payer_id,
        amount="0.10",
        key="round-half-up",
    )

    # 5% of 0.10 RUB is exactly half a kopeck and rounds up; the 1% level is
    # sub-cent and therefore has no balance or audit movement.
    assert (await UserService(uow).get(direct_id)).balance == Decimal("0.01")
    assert (await UserService(uow).get(root_id)).balance == Decimal("0.00")
    async with uow() as repos:
        rewards = (await repos["users"].session.scalars(select(ReferralReward))).all()
    assert [(row.level, row.reward_amount) for row in rewards] == [(1, Decimal("0.01"))]


@pytest.mark.asyncio
async def test_referral_kill_switch_does_not_affect_main_credit(
    monkeypatch, sessionmaker
):
    root_id, payer_id = await _referral_chain(8401, 8402)
    billing = BillingService(uow, per_config_cost="1.00")
    monkeypatch.setattr(settings, "referral_rewards_enabled", False)

    receipt = await _capture(
        billing,
        user_id=payer_id,
        amount="100.00",
        key="rewards-disabled",
    )

    assert receipt.credited is True
    assert (await UserService(uow).get(payer_id)).balance == Decimal("100.00")
    assert (await UserService(uow).get(root_id)).balance == Decimal("0.00")
    async with uow() as repos:
        payment = await repos["users"].session.scalar(select(ProviderPayment))
        rewards = (await repos["users"].session.scalars(select(ReferralReward))).all()
    assert payment.status == "credited"
    assert payment.referral_settled_at is None
    assert rewards == []

    monkeypatch.setattr(settings, "referral_rewards_enabled", True)
    assert await billing.reconcile_referral_rewards() == 1
    assert await billing.reconcile_referral_rewards() == 0
    assert (await UserService(uow).get(root_id)).balance == Decimal("5.00")
    async with uow() as repos:
        payment = await repos["users"].session.scalar(select(ProviderPayment))
        rewards = (await repos["users"].session.scalars(select(ReferralReward))).all()
    assert payment.referral_settlement_status == "rewarded"
    assert len(rewards) == 1


@pytest.mark.asyncio
async def test_corrupt_cycle_fails_closed_for_rewards_but_credits_payer(sessionmaker):
    first_id, second_id = await _referral_chain(8501, 8502)
    async with uow() as repos:
        first = await repos["users"].get(id=first_id)
        first.referred_by_id = second_id

    receipt = await _capture(
        BillingService(uow, per_config_cost="1.00"),
        user_id=second_id,
        amount="100.00",
        key="cycle-safe",
    )

    assert receipt.credited is True
    assert (await UserService(uow).get(second_id)).balance == Decimal("100.00")
    assert (await UserService(uow).get(first_id)).balance == Decimal("0.00")
    async with uow() as repos:
        payment = await repos["users"].session.scalar(select(ProviderPayment))
        rewards = (await repos["users"].session.scalars(select(ReferralReward))).all()
    assert payment.referral_settlement_status == "invalid_chain"
    assert rewards == []


@pytest.mark.asyncio
async def test_referral_settlement_marks_no_referrer_and_zero_reward(sessionmaker):
    billing = BillingService(uow, per_config_cost="1.00")
    payer = await UserService(uow).register(8601)
    await _capture(
        billing,
        user_id=payer.id,
        amount="100.00",
        key="no-referrer",
    )

    _root_id, tiny_payer_id = await _referral_chain(8602, 8603)
    await _capture(
        billing,
        user_id=tiny_payer_id,
        amount="0.01",
        key="zero-representable-reward",
    )

    async with uow() as repos:
        payments = (
            await repos["users"].session.scalars(
                select(ProviderPayment).order_by(ProviderPayment.id)
            )
        ).all()
    assert [payment.referral_settlement_status for payment in payments] == [
        "no_referrer",
        "zero_reward",
    ]
    assert all(payment.referral_settled_at is not None for payment in payments)


@pytest.mark.asyncio
async def test_reconciliation_restores_retroactive_reward_entitlement(
    monkeypatch, sessionmaker
):
    beneficiary_id, payer_id = await _referral_chain(8701, 8702)
    billing = BillingService(uow, per_config_cost="1.00")
    await _capture(
        billing,
        user_id=payer_id,
        amount="100.00",
        key="retroactive-entitlement-source",
    )

    async with uow() as repos:
        server = await repos["servers"].add(
            Server(
                name="retroactive-entitlement",
                ip="127.0.0.1",
                port=16290,
                host="vpn.test",
                monthly_cost=Decimal("0.00"),
                location="test",
                api_key="secret",
            )
        )
        config = await repos["configs"].add(
            VPN_Config(
                name="retroactive-reward-config",
                server_id=server.id,
                owner_id=beneficiary_id,
                display_name="Retroactive",
                suspended=True,
                desired_state="suspended",
                actual_state="suspended",
            )
        )
        config_id = config.id
        revoked = await repos["configs"].add(
            VPN_Config(
                name="retroactive-reward-revoked-config",
                server_id=server.id,
                owner_id=beneficiary_id,
                display_name="Revoked",
                suspended=True,
                desired_state="revoked",
                actual_state="revoked",
            )
        )
        revoked_id = revoked.id

    executions = []

    async def record_execution(operation_ids, *, owner_id=None):
        executions.append((operation_ids, owner_id))
        return len(operation_ids)

    monkeypatch.setattr(
        billing._config_service,
        "execute_operations",
        record_execution,
    )

    assert await billing.reconcile_referral_rewards() == 0
    async with uow() as repos:
        config = await repos["configs"].get(id=config_id)
        revoked = await repos["configs"].get(id=revoked_id)

    assert config.desired_state == "active"
    assert config.operation_id is not None
    assert revoked.desired_state == "revoked"
    assert revoked.operation_id is None
    assert executions == []


@pytest.mark.asyncio
async def test_reconciliation_rechecks_balance_before_retroactive_unsuspend(
    monkeypatch, sessionmaker
):
    beneficiary_id, payer_id = await _referral_chain(8801, 8802)
    billing = BillingService(uow, per_config_cost="1.00")
    await _capture(
        billing,
        user_id=payer_id,
        amount="100.00",
        key="stale-entitlement-source",
    )

    async with uow() as repos:
        server = await repos["servers"].add(
            Server(
                name="stale-entitlement",
                ip="127.0.0.1",
                port=16290,
                host="vpn.test",
                monthly_cost=Decimal("0.00"),
                location="test",
                api_key="secret",
            )
        )
        config = await repos["configs"].add(
            VPN_Config(
                name="stale-entitlement-config",
                server_id=server.id,
                owner_id=beneficiary_id,
                display_name="Stale",
                suspended=True,
                desired_state="suspended",
                actual_state="suspended",
            )
        )
        config_id = config.id

    original_candidates = BillingRepo.list_referral_entitlement_candidate_ids

    async def candidates_then_spend_reward(self, *, limit=100):
        owner_ids = await original_candidates(self, limit=limit)
        if beneficiary_id in owner_ids:
            await self.apply_balance_change(
                user_id=beneficiary_id,
                amount="-5.00",
                kind=LedgerKind.MANUAL_WITHDRAWAL,
                idempotency_key="spend-reward-before-entitlement-recheck",
                allow_negative_balance=False,
            )
        return owner_ids

    monkeypatch.setattr(
        BillingRepo,
        "list_referral_entitlement_candidate_ids",
        candidates_then_spend_reward,
    )

    assert await billing.reconcile_referral_rewards() == 0
    async with uow() as repos:
        user = await repos["users"].get(id=beneficiary_id)
        config = await repos["configs"].get(id=config_id)

    assert user.balance == Decimal("0.00")
    assert config.desired_state == "suspended"
    assert config.operation_id is None


@pytest.mark.asyncio
async def test_catch_up_rejects_inconsistent_source_payment_ledger(
    monkeypatch, sessionmaker
):
    beneficiary_id, payer_id = await _referral_chain(8901, 8902)
    billing = BillingService(uow, per_config_cost="1.00")
    monkeypatch.setattr(settings, "referral_rewards_enabled", False)
    await _capture(
        billing,
        user_id=payer_id,
        amount="100.00",
        key="inconsistent-source-ledger",
    )
    await _capture(
        billing,
        user_id=payer_id,
        amount="100.00",
        key="valid-payment-after-inconsistent-source",
    )
    async with uow() as repos:
        payment = await repos["users"].session.scalar(
            select(ProviderPayment).where(
                ProviderPayment.provider_payment_id == "inconsistent-source-ledger"
            )
        )
        payment.amount = Decimal("200.00")

    monkeypatch.setattr(settings, "referral_rewards_enabled", True)
    with pytest.raises(InvalidOperationError, match="1 referral payment.*quarantined"):
        await billing.reconcile_referral_rewards()

    assert (await UserService(uow).get(beneficiary_id)).balance == Decimal("5.00")
    async with uow() as repos:
        payments = (
            await repos["users"].session.scalars(
                select(ProviderPayment).order_by(ProviderPayment.id)
            )
        ).all()
        rewards = (await repos["users"].session.scalars(select(ReferralReward))).all()
    assert [payment.referral_settlement_status for payment in payments] == [
        "invalid_accounting",
        "rewarded",
    ]
    assert len(rewards) == 1


@pytest.mark.asyncio
async def test_catch_up_rejects_inconsistent_existing_reward_ledger(sessionmaker):
    _beneficiary_id, payer_id = await _referral_chain(9001, 9002)
    billing = BillingService(uow, per_config_cost="1.00")
    await _capture(
        billing,
        user_id=payer_id,
        amount="100.00",
        key="inconsistent-existing-reward",
    )
    async with uow() as repos:
        session = repos["users"].session
        payment = await session.scalar(select(ProviderPayment))
        reward = await session.scalar(select(ReferralReward))
        reward_ledger = await session.get(LedgerEntry, reward.ledger_entry_id)
        payment.referral_settled_at = None
        payment.referral_program_version = None
        payment.referral_settlement_status = None
        reward_ledger.kind = LedgerKind.MANUAL_TOP_UP.value

    with pytest.raises(InvalidOperationError, match="1 referral payment.*quarantined"):
        await billing.reconcile_referral_rewards()

    async with uow() as repos:
        payment = await repos["users"].session.scalar(select(ProviderPayment))
        rewards = (await repos["users"].session.scalars(select(ReferralReward))).all()
    assert payment.referral_settlement_status == "invalid_accounting"
    assert payment.referral_settled_at is not None
    assert len(rewards) == 1
