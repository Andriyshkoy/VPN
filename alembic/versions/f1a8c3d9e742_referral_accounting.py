"""add invite codes and immutable referral accounting

Revision ID: f1a8c3d9e742
Revises: c3a6f1e8b902
Create Date: 2026-07-13 00:00:00.000000
"""

from __future__ import annotations

import secrets
from decimal import ROUND_HALF_UP, Decimal
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "f1a8c3d9e742"
down_revision: Union[str, None] = "c3a6f1e8b902"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PROGRAM_VERSION = "v1-5pct-1pct"
MONEY_QUANTUM = Decimal("0.01")
LEVEL_RATES_BPS = ((1, 500), (2, 100))


def _money(value: Decimal, rate_bps: int) -> Decimal:
    return (Decimal(value) * Decimal(rate_bps) / Decimal(10_000)).quantize(
        MONEY_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


def _backfill_referral_codes(bind) -> None:
    user = sa.table(
        "user",
        sa.column("id", sa.Integer()),
        sa.column("referral_code", sa.String()),
    )
    generated = set(
        bind.execute(
            sa.select(user.c.referral_code).where(user.c.referral_code.is_not(None))
        ).scalars()
    )
    user_ids = bind.execute(
        sa.select(user.c.id).where(user.c.referral_code.is_(None)).order_by(user.c.id)
    ).scalars()
    for user_id in user_ids:
        code = secrets.token_urlsafe(24)
        while code in generated:
            code = secrets.token_urlsafe(24)
        generated.add(code)
        bind.execute(
            sa.update(user).where(user.c.id == user_id).values(referral_code=code)
        )


def _assert_acyclic_referrals(bind) -> None:
    rows = bind.execute(sa.text('SELECT id, referred_by_id FROM "user"')).mappings()
    referrers = {int(row["id"]): row["referred_by_id"] for row in rows}
    for origin_id in referrers:
        seen = {origin_id}
        current_id = origin_id
        while referrers.get(current_id) is not None:
            next_id = int(referrers[current_id])
            if next_id in seen:
                raise RuntimeError(
                    "Referral migration refused: existing referral cycle detected "
                    f"from user {origin_id}"
                )
            if next_id not in referrers:
                raise RuntimeError(
                    "Referral migration refused: referral points to a missing user"
                )
            seen.add(next_id)
            current_id = next_id


def _install_referral_attribution_guard() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION vpn_reject_referral_attribution_mutation()
            RETURNS trigger AS $$
            BEGIN
                IF OLD.referred_by_id IS DISTINCT FROM NEW.referred_by_id THEN
                    RAISE EXCEPTION 'user referral attribution is immutable';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_user_referral_attribution_immutable
            BEFORE UPDATE OF referred_by_id ON "user"
            FOR EACH ROW
            EXECUTE FUNCTION vpn_reject_referral_attribution_mutation()
            """
        )
    )


def _drop_referral_attribution_guard() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        sa.text(
            'DROP TRIGGER IF EXISTS trg_user_referral_attribution_immutable ON "user"'
        )
    )
    op.execute(
        sa.text("DROP FUNCTION IF EXISTS vpn_reject_referral_attribution_mutation()")
    )


def _install_referral_cycle_guard() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION vpn_reject_referral_cycle()
            RETURNS trigger AS $$
            DECLARE
                cycle_found boolean;
            BEGIN
                IF NEW.referred_by_id IS NULL THEN
                    RETURN NEW;
                END IF;
                WITH RECURSIVE ancestry(id) AS (
                    SELECT NEW.referred_by_id
                    UNION
                    SELECT parent.referred_by_id
                    FROM "user" AS parent
                    JOIN ancestry ON parent.id = ancestry.id
                    WHERE parent.referred_by_id IS NOT NULL
                )
                SELECT EXISTS(
                    SELECT 1 FROM ancestry WHERE id = NEW.id
                ) INTO cycle_found;
                IF cycle_found THEN
                    RAISE EXCEPTION 'user referral cycle is forbidden';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE CONSTRAINT TRIGGER trg_user_referral_acyclic
            AFTER INSERT ON "user"
            DEFERRABLE INITIALLY IMMEDIATE
            FOR EACH ROW EXECUTE FUNCTION vpn_reject_referral_cycle()
            """
        )
    )


def _drop_referral_cycle_guard() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(sa.text('DROP TRIGGER IF EXISTS trg_user_referral_acyclic ON "user"'))
    op.execute(sa.text("DROP FUNCTION IF EXISTS vpn_reject_referral_cycle()"))


def _install_referral_reward_guard() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION vpn_reject_referral_reward_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'referral_reward rows are immutable';
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_referral_reward_immutable
            BEFORE UPDATE OR DELETE ON referral_reward
            FOR EACH ROW EXECUTE FUNCTION vpn_reject_referral_reward_mutation()
            """
        )
    )


def _drop_referral_reward_guard() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS trg_referral_reward_immutable " "ON referral_reward"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS vpn_reject_referral_reward_mutation()"))


def _assert_payments_are_backfillable(bind) -> None:
    incomplete = bind.scalar(
        sa.text(
            "SELECT count(*) FROM provider_payment "
            "WHERE status = 'credited' AND ledger_entry_id IS NULL"
        )
    )
    if incomplete:
        raise RuntimeError(
            "Referral migration refused: credited provider payments without ledger"
        )

    unsupported_currency = bind.scalar(
        sa.text(
            "SELECT count(*) FROM provider_payment "
            "WHERE status = 'credited' AND currency <> 'RUB'"
        )
    )
    if unsupported_currency:
        raise RuntimeError(
            "Referral migration refused: non-RUB credited payments require an "
            "explicit conversion policy"
        )

    invalid_ledger = bind.scalar(
        sa.text(
            "SELECT count(*) FROM provider_payment AS payment "
            "LEFT JOIN ledger_entry AS ledger "
            "ON ledger.id = payment.ledger_entry_id "
            "WHERE payment.status = 'credited' AND ("
            "ledger.id IS NULL "
            "OR ledger.user_id <> payment.user_id "
            "OR ledger.kind <> 'provider_payment' "
            "OR ledger.amount <> payment.amount "
            "OR ledger.reference_type IS DISTINCT FROM 'provider_payment' "
            "OR ledger.reference_id IS DISTINCT FROM CAST(payment.id AS VARCHAR))"
        )
    )
    if invalid_ledger:
        raise RuntimeError(
            "Referral migration refused: credited payment ledger is inconsistent"
        )


def _backfill_historical_rewards(bind) -> None:
    _assert_payments_are_backfillable(bind)

    user = sa.table(
        "user",
        sa.column("id", sa.Integer()),
        sa.column("referred_by_id", sa.Integer()),
        sa.column("balance", sa.Numeric(18, 2)),
    )
    payment = sa.table(
        "provider_payment",
        sa.column("id", sa.Integer()),
        sa.column("user_id", sa.Integer()),
        sa.column("amount", sa.Numeric(18, 2)),
        sa.column("currency", sa.String()),
        sa.column("status", sa.String()),
        sa.column("ledger_entry_id", sa.Integer()),
        sa.column("credited_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("referral_settled_at", sa.DateTime(timezone=True)),
        sa.column("referral_program_version", sa.String()),
        sa.column("referral_settlement_status", sa.String()),
    )
    ledger = sa.table(
        "ledger_entry",
        sa.column("id", sa.Integer()),
        sa.column("user_id", sa.Integer()),
        sa.column("amount", sa.Numeric(18, 2)),
        sa.column("balance_after", sa.Numeric(18, 2)),
        sa.column("kind", sa.String()),
        sa.column("idempotency_key", sa.String()),
        sa.column("reference_type", sa.String()),
        sa.column("reference_id", sa.String()),
        sa.column("details", sa.JSON()),
    )
    reward = sa.table(
        "referral_reward",
        sa.column("source_payment_id", sa.Integer()),
        sa.column("source_user_id", sa.Integer()),
        sa.column("beneficiary_user_id", sa.Integer()),
        sa.column("level", sa.SmallInteger()),
        sa.column("rate_bps", sa.SmallInteger()),
        sa.column("source_amount", sa.Numeric(18, 2)),
        sa.column("reward_amount", sa.Numeric(18, 2)),
        sa.column("currency", sa.String()),
        sa.column("ledger_entry_id", sa.Integer()),
        sa.column("program_version", sa.String()),
    )

    user_rows = bind.execute(
        sa.select(user.c.id, user.c.referred_by_id, user.c.balance)
    ).mappings()
    users = {
        int(row["id"]): {
            "referred_by_id": row["referred_by_id"],
            "balance": Decimal(row["balance"]),
        }
        for row in user_rows
    }
    payments = bind.execute(
        sa.select(
            payment.c.id,
            payment.c.user_id,
            payment.c.amount,
            payment.c.currency,
            payment.c.referral_settled_at,
            payment.c.referral_program_version,
            payment.c.referral_settlement_status,
        )
        .where(
            payment.c.status == "credited",
            payment.c.ledger_entry_id.is_not(None),
        )
        .order_by(
            sa.func.coalesce(payment.c.credited_at, payment.c.created_at),
            payment.c.id,
        )
    ).mappings()

    for payment_row in payments:
        payment_id = int(payment_row["id"])
        source_user_id = int(payment_row["user_id"])
        source_user = users.get(source_user_id)
        if source_user is None:
            raise RuntimeError(
                "Referral migration refused: provider payment owner is missing"
            )

        level_one_id = source_user["referred_by_id"]
        level_one_user = (
            users.get(int(level_one_id)) if level_one_id is not None else None
        )
        if level_one_id is not None and level_one_user is None:
            raise RuntimeError(
                "Referral migration refused: level-one beneficiary is missing"
            )
        beneficiaries = (
            int(level_one_id) if level_one_id is not None else None,
            (
                int(level_one_user["referred_by_id"])
                if level_one_user is not None
                and level_one_user["referred_by_id"] is not None
                else None
            ),
        )

        rewarded_levels = 0
        for (level, rate_bps), beneficiary_id in zip(
            LEVEL_RATES_BPS, beneficiaries, strict=True
        ):
            if beneficiary_id is None:
                continue
            if beneficiary_id == source_user_id:
                raise RuntimeError(
                    "Referral migration refused: payment would reward its payer"
                )
            beneficiary = users.get(beneficiary_id)
            if beneficiary is None:
                raise RuntimeError(
                    "Referral migration refused: reward beneficiary is missing"
                )

            source_amount = Decimal(payment_row["amount"])
            reward_amount = _money(source_amount, rate_bps)
            if reward_amount == 0:
                continue

            idempotency_key = (
                f"referral-reward:v1:provider-payment:{payment_id}:level:{level}"
            )
            existing_ledger = (
                bind.execute(
                    sa.select(
                        ledger.c.id,
                        ledger.c.user_id,
                        ledger.c.amount,
                        ledger.c.kind,
                        ledger.c.reference_type,
                        ledger.c.reference_id,
                    ).where(ledger.c.idempotency_key == idempotency_key)
                )
                .mappings()
                .one_or_none()
            )
            existing_reward = (
                bind.execute(
                    sa.select(
                        reward.c.source_payment_id,
                        reward.c.source_user_id,
                        reward.c.beneficiary_user_id,
                        reward.c.level,
                        reward.c.rate_bps,
                        reward.c.source_amount,
                        reward.c.reward_amount,
                        reward.c.currency,
                        reward.c.ledger_entry_id,
                        reward.c.program_version,
                    ).where(
                        reward.c.source_payment_id == payment_id,
                        reward.c.level == level,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing_ledger is not None and existing_reward is not None:
                valid_existing = (
                    int(existing_ledger["id"])
                    == int(existing_reward["ledger_entry_id"])
                    and int(existing_ledger["user_id"]) == beneficiary_id
                    and Decimal(existing_ledger["amount"]) == reward_amount
                    and existing_ledger["kind"] == f"referral_reward_l{level}"
                    and existing_ledger["reference_type"] == "referral_reward"
                    and existing_ledger["reference_id"]
                    == f"payment:{payment_id}:level:{level}"
                    and int(existing_reward["source_user_id"]) == source_user_id
                    and int(existing_reward["beneficiary_user_id"]) == beneficiary_id
                    and int(existing_reward["rate_bps"]) == rate_bps
                    and Decimal(existing_reward["source_amount"]) == source_amount
                    and Decimal(existing_reward["reward_amount"]) == reward_amount
                    and existing_reward["currency"] == payment_row["currency"]
                    and existing_reward["program_version"] == PROGRAM_VERSION
                )
                if not valid_existing:
                    raise RuntimeError(
                        "Referral migration refused: existing historical reward "
                        "does not match the configured program"
                    )
                rewarded_levels += 1
                continue
            if existing_ledger is not None or existing_reward is not None:
                raise RuntimeError(
                    "Referral migration refused: partial historical reward detected"
                )

            balance_after = Decimal(beneficiary["balance"]) + reward_amount
            bind.execute(
                sa.update(user)
                .where(user.c.id == beneficiary_id)
                .values(balance=balance_after)
            )
            beneficiary["balance"] = balance_after

            details = {
                "source_payment_id": payment_id,
                "source_user_id": source_user_id,
                "beneficiary_user_id": beneficiary_id,
                "level": level,
                "rate_bps": rate_bps,
                "currency": str(payment_row["currency"]),
                "program_version": PROGRAM_VERSION,
                "retroactive": True,
            }
            ledger_entry_id = bind.execute(
                sa.insert(ledger)
                .values(
                    user_id=beneficiary_id,
                    amount=reward_amount,
                    balance_after=balance_after,
                    kind=f"referral_reward_l{level}",
                    idempotency_key=idempotency_key,
                    reference_type="referral_reward",
                    reference_id=f"payment:{payment_id}:level:{level}",
                    details=details,
                )
                .returning(ledger.c.id)
            ).scalar_one()
            bind.execute(
                sa.insert(reward).values(
                    source_payment_id=payment_id,
                    source_user_id=source_user_id,
                    beneficiary_user_id=beneficiary_id,
                    level=level,
                    rate_bps=rate_bps,
                    source_amount=source_amount,
                    reward_amount=reward_amount,
                    currency=str(payment_row["currency"]),
                    ledger_entry_id=ledger_entry_id,
                    program_version=PROGRAM_VERSION,
                )
            )
            rewarded_levels += 1

        settlement_status = (
            "rewarded"
            if rewarded_levels
            else ("no_referrer" if level_one_id is None else "zero_reward")
        )
        existing_settlement = (
            payment_row["referral_settled_at"],
            payment_row["referral_program_version"],
            payment_row["referral_settlement_status"],
        )
        if any(value is not None for value in existing_settlement):
            if not all(value is not None for value in existing_settlement):
                raise RuntimeError(
                    "Referral migration refused: partial payment settlement marker"
                )
            if existing_settlement[1:] != (PROGRAM_VERSION, settlement_status):
                raise RuntimeError(
                    "Referral migration refused: existing payment settlement does "
                    "not match the configured program"
                )
        else:
            bind.execute(
                sa.update(payment)
                .where(payment.c.id == payment_id)
                .values(
                    referral_settled_at=sa.func.now(),
                    referral_program_version=PROGRAM_VERSION,
                    referral_settlement_status=settlement_status,
                )
            )


def upgrade() -> None:
    bind = op.get_bind()

    op.add_column(
        "user", sa.Column("referral_code", sa.String(length=32), nullable=True)
    )
    _backfill_referral_codes(bind)
    op.alter_column(
        "user",
        "referral_code",
        existing_type=sa.String(length=32),
        nullable=False,
    )
    op.create_index("ix_user_referral_code", "user", ["referral_code"], unique=True)

    op.add_column(
        "provider_payment",
        sa.Column("referral_settled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "provider_payment",
        sa.Column("referral_program_version", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "provider_payment",
        sa.Column("referral_settlement_status", sa.String(length=24), nullable=True),
    )
    op.create_check_constraint(
        "ck_provider_payment_referral_settlement_status",
        "provider_payment",
        "referral_settlement_status IS NULL OR referral_settlement_status IN "
        "('rewarded', 'no_referrer', 'zero_reward', 'invalid_chain', "
        "'invalid_accounting')",
    )
    op.create_check_constraint(
        "ck_provider_payment_referral_settlement_complete",
        "provider_payment",
        "(referral_settled_at IS NULL AND referral_program_version IS NULL "
        "AND referral_settlement_status IS NULL) OR "
        "(status = 'credited' AND ledger_entry_id IS NOT NULL "
        "AND referral_settled_at IS NOT NULL "
        "AND referral_program_version IS NOT NULL "
        "AND referral_settlement_status IS NOT NULL)",
    )
    op.create_index(
        "ix_provider_payment_referral_settlement",
        "provider_payment",
        ["status", "referral_settled_at", "id"],
    )

    _assert_acyclic_referrals(bind)
    op.create_check_constraint(
        "ck_user_not_self_referred",
        "user",
        "referred_by_id IS NULL OR referred_by_id <> id",
    )
    _install_referral_attribution_guard()
    _install_referral_cycle_guard()

    op.create_table(
        "referral_reward",
        sa.Column("source_payment_id", sa.Integer(), nullable=False),
        sa.Column("source_user_id", sa.Integer(), nullable=False),
        sa.Column("beneficiary_user_id", sa.Integer(), nullable=False),
        sa.Column("level", sa.SmallInteger(), nullable=False),
        sa.Column("rate_bps", sa.SmallInteger(), nullable=False),
        sa.Column("source_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("reward_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("ledger_entry_id", sa.Integer(), nullable=False),
        sa.Column(
            "program_version",
            sa.String(length=32),
            nullable=False,
            server_default=PROGRAM_VERSION,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.CheckConstraint("level IN (1, 2)", name="ck_referral_reward_level"),
        sa.CheckConstraint(
            "rate_bps > 0 AND rate_bps <= 10000",
            name="ck_referral_reward_rate_bps",
        ),
        sa.CheckConstraint(
            "source_amount > 0", name="ck_referral_reward_positive_source_amount"
        ),
        sa.CheckConstraint(
            "reward_amount > 0", name="ck_referral_reward_positive_reward_amount"
        ),
        sa.CheckConstraint(
            "source_user_id <> beneficiary_user_id",
            name="ck_referral_reward_not_self",
        ),
        sa.ForeignKeyConstraint(
            ["source_payment_id"], ["provider_payment.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["source_user_id"], ["user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["beneficiary_user_id"], ["user.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["ledger_entry_id"], ["ledger_entry.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_payment_id",
            "level",
            name="uq_referral_reward_payment_level",
        ),
        sa.UniqueConstraint(
            "source_payment_id",
            "beneficiary_user_id",
            name="uq_referral_reward_payment_beneficiary",
        ),
        sa.UniqueConstraint(
            "ledger_entry_id", name="uq_referral_reward_ledger_entry_id"
        ),
    )
    op.create_index(
        "ix_referral_reward_source_payment_id",
        "referral_reward",
        ["source_payment_id"],
    )
    op.create_index(
        "ix_referral_reward_source_user_id",
        "referral_reward",
        ["source_user_id"],
    )
    op.create_index(
        "ix_referral_reward_beneficiary_user_id",
        "referral_reward",
        ["beneficiary_user_id"],
    )
    op.create_index(
        "ix_referral_reward_created_at",
        "referral_reward",
        ["created_at"],
    )
    _install_referral_reward_guard()
    _backfill_historical_rewards(bind)


def downgrade() -> None:
    bind = op.get_bind()
    rewards = bind.scalar(sa.text("SELECT count(*) FROM referral_reward"))
    ledger_rewards = bind.scalar(
        sa.text(
            "SELECT count(*) FROM ledger_entry "
            "WHERE kind IN ('referral_reward_l1', 'referral_reward_l2')"
        )
    )
    settled_payments = bind.scalar(
        sa.text(
            "SELECT count(*) FROM provider_payment "
            "WHERE referral_settled_at IS NOT NULL "
            "OR referral_program_version IS NOT NULL "
            "OR referral_settlement_status IS NOT NULL"
        )
    )
    if rewards or ledger_rewards or settled_payments:
        raise RuntimeError(
            "Unsafe referral accounting downgrade refused; settlement facts, "
            "reward balances, and immutable ledger history must be retained"
        )

    _drop_referral_reward_guard()
    op.drop_index("ix_referral_reward_created_at", table_name="referral_reward")
    op.drop_index(
        "ix_referral_reward_beneficiary_user_id", table_name="referral_reward"
    )
    op.drop_index("ix_referral_reward_source_user_id", table_name="referral_reward")
    op.drop_index("ix_referral_reward_source_payment_id", table_name="referral_reward")
    op.drop_table("referral_reward")

    _drop_referral_cycle_guard()
    _drop_referral_attribution_guard()
    op.drop_constraint("ck_user_not_self_referred", "user", type_="check")
    op.drop_index("ix_user_referral_code", table_name="user")
    op.drop_column("user", "referral_code")

    op.drop_index(
        "ix_provider_payment_referral_settlement",
        table_name="provider_payment",
    )
    op.drop_constraint(
        "ck_provider_payment_referral_settlement_complete",
        "provider_payment",
        type_="check",
    )
    op.drop_constraint(
        "ck_provider_payment_referral_settlement_status",
        "provider_payment",
        type_="check",
    )
    op.drop_column("provider_payment", "referral_settlement_status")
    op.drop_column("provider_payment", "referral_program_version")
    op.drop_column("provider_payment", "referral_settled_at")
