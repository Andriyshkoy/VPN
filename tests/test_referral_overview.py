from decimal import Decimal

import pytest

from core.db.models import User
from core.db.unit_of_work import uow
from core.services import BillingService, ReferralService


@pytest.mark.asyncio
async def test_referral_overview_returns_only_private_aggregates(sessionmaker):
    async with uow() as repos:
        root = await repos["users"].add(User(tg_id=95_001, username="root"))
        direct = await repos["users"].add(
            User(tg_id=95_002, username="direct", referred_by_id=root.id)
        )
        payer = await repos["users"].add(
            User(tg_id=95_003, username="payer", referred_by_id=direct.id)
        )
        await repos["users"].add(
            User(tg_id=95_004, username="second", referred_by_id=direct.id)
        )

    billing = BillingService(uow, per_config_cost="1.00")
    intent = await billing.create_payment_intent(
        user_id=payer.id,
        amount="500.00",
    )
    await billing.record_provider_payment(
        user_id=payer.id,
        provider="telegram",
        provider_payment_id="overview-payment",
        amount="500.00",
        currency="RUB",
        payload=intent.payload,
        intent_id=intent.intent_id,
    )

    root_overview = await ReferralService(uow).overview(root.id)
    direct_overview = await ReferralService(uow).overview(direct.id)

    assert root_overview.referral_code == root.referral_code
    assert root_overview.level_1_count == 1
    assert root_overview.level_2_count == 2
    assert root_overview.level_1_earned == Decimal("0.00")
    assert root_overview.level_2_earned == Decimal("5.00")
    assert root_overview.total_earned == Decimal("5.00")

    assert direct_overview.level_1_count == 2
    assert direct_overview.level_2_count == 0
    assert direct_overview.level_1_earned == Decimal("25.00")
    assert direct_overview.level_2_earned == Decimal("0.00")
    assert direct_overview.total_earned == Decimal("25.00")
