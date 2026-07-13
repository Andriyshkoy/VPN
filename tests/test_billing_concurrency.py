from __future__ import annotations

import asyncio
import os
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.db import Base
from core.db.models.config import VPN_Config
from core.db.models.ledger import LedgerEntry, LedgerKind
from core.db.models.payment import ProviderPayment
from core.db.models.user import User
from core.db.models.vpn_operation import VPNOperation
from core.db.repo.billing import BillingRepo
from core.db.unit_of_work import uow
from core.exceptions import InvalidOperationError
from core.services import BillingService, ServerService, UserService
from core.services.billing import PaymentIntent

POSTGRES_TEST_URL = os.getenv("POSTGRES_TEST_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="POSTGRES_TEST_URL is required for locking/concurrency tests",
)


@pytest.mark.asyncio
async def test_atomic_updates_and_duplicate_key_under_postgres():
    engine = create_async_engine(POSTGRES_TEST_URL, echo=False)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)

        async with maker() as session, session.begin():
            db_user = User(tg_id=9001, balance=Decimal("0.00"))
            session.add(db_user)
            await session.flush()
            user_id = db_user.id

        async def credit(key: str):
            async with maker() as session, session.begin():
                return await BillingRepo(session).apply_balance_change(
                    user_id=user_id,
                    amount="0.10",
                    kind=LedgerKind.MANUAL_TOP_UP,
                    idempotency_key=key,
                    allow_negative_balance=True,
                )

        # Different operations must not lose either increment while contending
        # for the same user row.
        await asyncio.gather(credit("concurrent:a"), credit("concurrent:b"))
        async with maker() as session:
            assert (await session.get(User, user_id)).balance == Decimal("0.20")

        # Duplicate delivery may race, but only one transaction may change the
        # balance and append the unique ledger record.
        results = await asyncio.gather(
            credit("concurrent:duplicate"), credit("concurrent:duplicate")
        )
        assert sorted(result.applied for result in results) == [False, True]
        async with maker() as session:
            assert (await session.get(User, user_id)).balance == Decimal("0.30")
            count = await session.scalar(
                select(func.count())
                .select_from(LedgerEntry)
                .where(LedgerEntry.user_id == user_id)
            )
            assert count == 3
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_paid_config_replay_creates_and_charges_once(monkeypatch):
    engine = create_async_engine(POSTGRES_TEST_URL, echo=False)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    class Gateway:
        def __init__(self) -> None:
            self.create_calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def create_client(self, *args, **kwargs):
            self.create_calls += 1
            await asyncio.sleep(0.05)

        async def download_config(self, *args, **kwargs):
            return b"profile"

    gateway = Gateway()
    monkeypatch.setattr("core.db.unit_of_work.async_session", maker)
    monkeypatch.setattr(
        "core.services.config.APIGateway",
        lambda *args, **kwargs: gateway,
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)

        user = await UserService(uow).register(9002, balance="20.00")
        server = await ServerService(uow).create(
            name="paid-replay",
            ip="127.0.0.1",
            port=16290,
            host="vpn.test",
            location="test",
            api_key="secret",
            cost=0,
        )
        billing = BillingService(uow, per_config_cost="1.00")

        async def purchase():
            return await billing.create_paid_config(
                server_id=server.id,
                owner_id=user.id,
                name="stable-telegram-update",
                display_name="Phone",
                creation_cost="10.00",
                idempotency_key="telegram:create-config:update:9002",
            )

        first, second = await asyncio.gather(purchase(), purchase())

        assert first.id == second.id
        assert gateway.create_calls == 1

        async def invoice():
            return await billing.create_payment_intent(
                user_id=user.id,
                amount="100.00",
                provider="telegram",
                currency="RUB",
                idempotency_key="telegram:invoice:update:9002",
            )

        first_invoice, second_invoice = await asyncio.gather(invoice(), invoice())
        assert first_invoice.intent_id == second_invoice.intent_id

        async def claim_invoice_delivery():
            return await billing.claim_payment_invoice_delivery(
                user_id=user.id,
                intent_id=first_invoice.intent_id,
            )

        claims = await asyncio.gather(
            claim_invoice_delivery(),
            claim_invoice_delivery(),
        )
        assert sorted(claims) == [False, True]

        async def claim_checkout(claim_id: str):
            return await billing.validate_payment_intent(
                user_id=user.id,
                claim_id=claim_id,
                payload=first_invoice.payload,
                amount=first_invoice.amount,
                currency=first_invoice.currency,
            )

        checkout_claims = await asyncio.gather(
            claim_checkout("checkout-a"),
            claim_checkout("checkout-b"),
            return_exceptions=True,
        )
        assert sum(isinstance(result, PaymentIntent) for result in checkout_claims) == 1
        assert (
            sum(isinstance(result, InvalidOperationError) for result in checkout_claims)
            == 1
        )
        async with maker() as session:
            assert (await session.get(User, user.id)).balance == Decimal("10.00")
            assert (
                await session.scalar(select(func.count()).select_from(VPN_Config)) == 1
            )
            assert (
                await session.scalar(select(func.count()).select_from(VPNOperation))
                == 1
            )
            assert (
                await session.scalar(select(func.count()).select_from(ProviderPayment))
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(LedgerEntry)
                    .where(LedgerEntry.kind == LedgerKind.CONFIG_RESERVATION.value)
                )
                == 1
            )
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()
