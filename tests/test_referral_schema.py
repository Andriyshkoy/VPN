from __future__ import annotations

import importlib.util
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from core.db.models.payment import ProviderPayment
from core.db.models.user import User


def _migration_module():
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "f1a8c3d9e742_referral_accounting.py"
    )
    spec = importlib.util.spec_from_file_location("referral_accounting_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _define_backfill_tables(metadata: sa.MetaData):
    user = sa.Table(
        "user",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("referred_by_id", sa.Integer, nullable=True),
        sa.Column("referral_code", sa.String(32), nullable=True),
        sa.Column("balance", sa.Numeric(18, 2), nullable=False),
    )
    ledger = sa.Table(
        "ledger_entry",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("balance_after", sa.Numeric(18, 2), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column("reference_type", sa.String(32), nullable=True),
        sa.Column("reference_id", sa.String(160), nullable=True),
        sa.Column("details", sa.JSON, nullable=False),
    )
    payment = sa.Table(
        "provider_payment",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("ledger_entry_id", sa.Integer, nullable=True),
        sa.Column("credited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("referral_settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("referral_program_version", sa.String(32), nullable=True),
        sa.Column("referral_settlement_status", sa.String(24), nullable=True),
    )
    reward = sa.Table(
        "referral_reward",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source_payment_id", sa.Integer, nullable=False),
        sa.Column("source_user_id", sa.Integer, nullable=False),
        sa.Column("beneficiary_user_id", sa.Integer, nullable=False),
        sa.Column("level", sa.SmallInteger, nullable=False),
        sa.Column("rate_bps", sa.SmallInteger, nullable=False),
        sa.Column("source_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("reward_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("ledger_entry_id", sa.Integer, nullable=False),
        sa.Column("program_version", sa.String(32), nullable=False),
    )
    return user, ledger, payment, reward


@pytest.mark.asyncio
async def test_user_referral_codes_are_opaque_unique_and_self_referral_is_rejected(
    session,
):
    first = User(tg_id=97001, balance=Decimal("0.00"))
    second = User(tg_id=97002, balance=Decimal("0.00"))
    session.add_all([first, second])
    await session.flush()

    assert re.fullmatch(r"[A-Za-z0-9_-]{32}", first.referral_code)
    assert re.fullmatch(r"[A-Za-z0-9_-]{32}", second.referral_code)
    assert first.referral_code != second.referral_code

    first.referred_by_id = first.id
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "settled_at", "program_version", "settlement_status"),
    [
        (
            "pending",
            datetime.now(timezone.utc),
            "v1-5pct-1pct",
            "no_referrer",
        ),
        ("credited", datetime.now(timezone.utc), None, "rewarded"),
        (
            "credited",
            datetime.now(timezone.utc),
            "v1-5pct-1pct",
            "no_referrer",
        ),
    ],
    ids=(
        "non-credited-settlement",
        "partial-settlement",
        "settlement-without-ledger",
    ),
)
async def test_provider_payment_rejects_invalid_referral_settlement_marker(
    session,
    status,
    settled_at,
    program_version,
    settlement_status,
):
    payer = User(tg_id=97100, balance=Decimal("0.00"))
    session.add(payer)
    await session.flush()
    session.add(
        ProviderPayment(
            intent_id=f"settlement-{status}-{program_version or 'missing'}",
            user_id=payer.id,
            provider="telegram",
            amount=Decimal("100.00"),
            currency="RUB",
            payload=f"payload-{status}-{program_version or 'missing'}",
            status=status,
            referral_settled_at=settled_at,
            referral_program_version=program_version,
            referral_settlement_status=settlement_status,
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


def test_historical_referral_backfill_is_idempotent_and_auditable():
    migration = _migration_module()
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    user, ledger, payment, reward = _define_backfill_tables(metadata)
    metadata.create_all(engine)

    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            sa.insert(user),
            [
                {"id": 1, "referred_by_id": None, "balance": Decimal("0.00")},
                {"id": 2, "referred_by_id": 1, "balance": Decimal("0.00")},
                {"id": 3, "referred_by_id": 2, "balance": Decimal("500.00")},
            ],
        )
        connection.execute(
            sa.insert(ledger).values(
                id=10,
                user_id=3,
                amount=Decimal("500.00"),
                balance_after=Decimal("500.00"),
                kind="provider_payment",
                idempotency_key="provider-payment:test:20",
                reference_type="provider_payment",
                reference_id="20",
                details={},
            )
        )
        connection.execute(
            sa.insert(payment).values(
                id=20,
                user_id=3,
                amount=Decimal("500.00"),
                currency="RUB",
                status="credited",
                ledger_entry_id=10,
                credited_at=now,
                created_at=now,
            )
        )

        migration._backfill_referral_codes(connection)
        codes = (
            connection.execute(sa.select(user.c.referral_code).order_by(user.c.id))
            .scalars()
            .all()
        )
        assert len(set(codes)) == 3
        assert all(re.fullmatch(r"[A-Za-z0-9_-]{32}", code) for code in codes)

        migration._assert_acyclic_referrals(connection)
        migration._backfill_historical_rewards(connection)
        fixed_settlement_at = datetime(2020, 1, 2)
        connection.execute(
            sa.update(payment)
            .where(payment.c.id == 20)
            .values(referral_settled_at=fixed_settlement_at)
        )
        migration._backfill_historical_rewards(connection)

        balances = connection.execute(
            sa.select(user.c.id, user.c.balance).order_by(user.c.id)
        ).all()
        rewards = connection.execute(
            sa.select(
                reward.c.level,
                reward.c.beneficiary_user_id,
                reward.c.rate_bps,
                reward.c.reward_amount,
            ).order_by(reward.c.level)
        ).all()
        reward_ledger = connection.execute(
            sa.select(ledger.c.kind, ledger.c.amount, ledger.c.details)
            .where(ledger.c.kind.like("referral_reward_l%"))
            .order_by(ledger.c.kind)
        ).all()

        assert balances == [
            (1, Decimal("5.00")),
            (2, Decimal("25.00")),
            (3, Decimal("500.00")),
        ]
        assert rewards == [
            (1, 2, 500, Decimal("25.00")),
            (2, 1, 100, Decimal("5.00")),
        ]
        assert [(row.kind, row.amount) for row in reward_ledger] == [
            ("referral_reward_l1", Decimal("25.00")),
            ("referral_reward_l2", Decimal("5.00")),
        ]
        assert all(row.details["retroactive"] is True for row in reward_ledger)
        settlement = connection.execute(
            sa.select(
                payment.c.referral_settled_at,
                payment.c.referral_program_version,
                payment.c.referral_settlement_status,
            )
        ).one()
        assert settlement == (
            fixed_settlement_at,
            "v1-5pct-1pct",
            "rewarded",
        )

        connection.execute(
            sa.update(user).where(user.c.id == 1).values(referred_by_id=3)
        )
        with pytest.raises(RuntimeError, match="cycle"):
            migration._assert_acyclic_referrals(connection)

    engine.dispose()


def test_historical_backfill_marks_non_rewarding_payments_as_settled():
    migration = _migration_module()
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    user, ledger, payment, _ = _define_backfill_tables(metadata)
    metadata.create_all(engine)

    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            sa.insert(user),
            [
                {"id": 1, "referred_by_id": None, "balance": Decimal("100.00")},
                {"id": 2, "referred_by_id": 1, "balance": Decimal("0.01")},
            ],
        )
        connection.execute(
            sa.insert(ledger),
            [
                {
                    "id": 10,
                    "user_id": 1,
                    "amount": Decimal("100.00"),
                    "balance_after": Decimal("100.00"),
                    "kind": "provider_payment",
                    "idempotency_key": "provider-payment:test:20",
                    "reference_type": "provider_payment",
                    "reference_id": "20",
                    "details": {},
                },
                {
                    "id": 11,
                    "user_id": 2,
                    "amount": Decimal("0.01"),
                    "balance_after": Decimal("0.01"),
                    "kind": "provider_payment",
                    "idempotency_key": "provider-payment:test:21",
                    "reference_type": "provider_payment",
                    "reference_id": "21",
                    "details": {},
                },
            ],
        )
        connection.execute(
            sa.insert(payment),
            [
                {
                    "id": 20,
                    "user_id": 1,
                    "amount": Decimal("100.00"),
                    "currency": "RUB",
                    "status": "credited",
                    "ledger_entry_id": 10,
                    "credited_at": now,
                    "created_at": now,
                },
                {
                    "id": 21,
                    "user_id": 2,
                    "amount": Decimal("0.01"),
                    "currency": "RUB",
                    "status": "credited",
                    "ledger_entry_id": 11,
                    "credited_at": now,
                    "created_at": now,
                },
            ],
        )

        migration._backfill_historical_rewards(connection)

        settlements = connection.execute(
            sa.select(
                payment.c.id,
                payment.c.referral_settled_at,
                payment.c.referral_program_version,
                payment.c.referral_settlement_status,
            ).order_by(payment.c.id)
        ).all()
        assert [(row.id, row.referral_settlement_status) for row in settlements] == [
            (20, "no_referrer"),
            (21, "zero_reward"),
        ]
        assert all(row.referral_settled_at is not None for row in settlements)
        assert all(
            row.referral_program_version == "v1-5pct-1pct" for row in settlements
        )

    engine.dispose()


@pytest.mark.parametrize(
    ("ledger_overrides", "insert_ledger"),
    [
        ({}, False),
        ({"user_id": 999}, True),
        ({"kind": "manual_top_up"}, True),
        ({"amount": Decimal("499.99")}, True),
        ({"reference_type": None}, True),
        ({"reference_id": "wrong-payment"}, True),
    ],
    ids=(
        "missing-ledger-row",
        "wrong-owner",
        "wrong-kind",
        "wrong-amount",
        "wrong-reference-type",
        "wrong-reference-id",
    ),
)
def test_historical_backfill_rejects_inconsistent_payment_ledger(
    ledger_overrides,
    insert_ledger,
):
    migration = _migration_module()
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    _, ledger, payment, _ = _define_backfill_tables(metadata)
    metadata.create_all(engine)

    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        ledger_values = {
            "id": 10,
            "user_id": 3,
            "amount": Decimal("500.00"),
            "balance_after": Decimal("500.00"),
            "kind": "provider_payment",
            "idempotency_key": "provider-payment:test:20",
            "reference_type": "provider_payment",
            "reference_id": "20",
            "details": {},
        }
        ledger_values.update(ledger_overrides)
        if insert_ledger:
            connection.execute(sa.insert(ledger).values(**ledger_values))
        connection.execute(
            sa.insert(payment).values(
                id=20,
                user_id=3,
                amount=Decimal("500.00"),
                currency="RUB",
                status="credited",
                ledger_entry_id=10,
                credited_at=now,
                created_at=now,
            )
        )

        with pytest.raises(RuntimeError, match="ledger is inconsistent"):
            migration._assert_payments_are_backfillable(connection)

    engine.dispose()


@pytest.mark.parametrize(
    ("currency", "ledger_entry_id", "error"),
    [
        ("RUB", None, "without ledger"),
        ("USD", 10, "non-RUB"),
    ],
)
def test_historical_backfill_rejects_unsupported_credited_payments(
    currency,
    ledger_entry_id,
    error,
):
    migration = _migration_module()
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    _, ledger, payment, _ = _define_backfill_tables(metadata)
    metadata.create_all(engine)

    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        if ledger_entry_id is not None:
            connection.execute(
                sa.insert(ledger).values(
                    id=10,
                    user_id=3,
                    amount=Decimal("500.00"),
                    balance_after=Decimal("500.00"),
                    kind="provider_payment",
                    idempotency_key="provider-payment:test:20",
                    reference_type="provider_payment",
                    reference_id="20",
                    details={},
                )
            )
        connection.execute(
            sa.insert(payment).values(
                id=20,
                user_id=3,
                amount=Decimal("500.00"),
                currency=currency,
                status="credited",
                ledger_entry_id=ledger_entry_id,
                credited_at=now,
                created_at=now,
            )
        )

        with pytest.raises(RuntimeError, match=error):
            migration._assert_payments_are_backfillable(connection)

    engine.dispose()
