\set ON_ERROR_STOP on

DO $$
DECLARE
    revision_count integer;
    current_revision text;
BEGIN
    SELECT count(*), min(version_num)
      INTO revision_count, current_revision
      FROM alembic_version;

    IF revision_count <> 1 OR current_revision <> 'f1a8c3d9e742' THEN
        RAISE EXCEPTION 'unexpected Alembic revision: %', current_revision;
    END IF;

    IF EXISTS (
        SELECT 1
          FROM "user"
         WHERE referral_code !~ '^[A-Za-z0-9_-]{32}$'
    ) THEN
        RAISE EXCEPTION 'invalid referral code detected';
    END IF;

    IF (
        SELECT count(*) FROM "user"
    ) <> (
        SELECT count(DISTINCT referral_code) FROM "user"
    ) THEN
        RAISE EXCEPTION 'referral codes are not unique';
    END IF;

    IF EXISTS (
        SELECT u.id
          FROM "user" AS u
          LEFT JOIN ledger_entry AS entry ON entry.user_id = u.id
         GROUP BY u.id, u.balance
        HAVING u.balance <> coalesce(sum(entry.amount), 0)
    ) THEN
        RAISE EXCEPTION 'user balance does not match ledger';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM provider_payment AS payment
          LEFT JOIN ledger_entry AS entry ON entry.id = payment.ledger_entry_id
         WHERE payment.status = 'credited'
           AND (
               payment.ledger_entry_id IS NULL
               OR entry.id IS NULL
               OR entry.user_id <> payment.user_id
               OR entry.kind <> 'provider_payment'
               OR entry.amount <> payment.amount
           )
    ) THEN
        RAISE EXCEPTION 'credited provider payment has invalid ledger ownership';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM referral_reward AS reward
          LEFT JOIN ledger_entry AS entry ON entry.id = reward.ledger_entry_id
         WHERE entry.id IS NULL
            OR entry.user_id <> reward.beneficiary_user_id
            OR entry.amount <> reward.reward_amount
            OR entry.kind <> ('referral_reward_l' || reward.level::text)
    ) THEN
        RAISE EXCEPTION 'referral reward has invalid ledger ownership';
    END IF;
END
$$;
