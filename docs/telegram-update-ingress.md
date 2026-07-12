# Durable Telegram update ingress

## Delivery contract

The bot still uses Telegram long polling, but receipt and handler execution are
separate:

1. `getUpdates` returns a batch.
2. The full JSON batch is committed to PostgreSQL.
3. Only after that commit does the in-memory poll offset advance.
4. A processor claims the oldest due inbox row using a renewable lease and a
   fencing token.
5. The existing aiogram dispatcher handles the reconstructed update.
6. Successful completion is recorded only by the current lease owner.

If the process dies before step 3, Telegram redelivers and the unique
`update_id` makes ingestion a no-op. If it dies during steps 4–6, the processing
lease expires and the row becomes claimable again. This is an at-least-once
contract, not exactly-once delivery: Telegram sends and other remote effects
cannot share a PostgreSQL transaction. Financial and VPN handlers must keep
their domain idempotency keys.

The bot runs a bounded processor pool (`TELEGRAM_UPDATE_PROCESSOR_COUNT`, four
by default). The repository enforces a head-of-line barrier per pseudonymous
conversation lane: if an earlier update for the same chat/user FSM is processing
or waiting for retry backoff, its later update cannot be claimed. Other
conversations continue normally. The lane is an HMAC derived with the stable
application encryption key; raw Telegram identifiers are not duplicated in the
inbox ordering column. Thus one lane remains strictly sequential even across
replicas, while Manager/Telegram latency in one conversation does not stop all
other users. A dead/processed terminal row no longer blocks its lane.
`TELEGRAM_UPDATE_HANDLER_TIMEOUT_SECONDS` must be shorter than
`TELEGRAM_UPDATE_LEASE_SECONDS`. A timeout cancels the handler before marking
the update failed for retry. If the heartbeat cannot renew or prove ownership,
the processor cancels the handler immediately and performs no stale ACK; the row
becomes recoverable after its existing lease expires.

## Operator checks

Inspect backlog and oldest age:

```sql
SELECT status, count(*), min(received_at) AS oldest
FROM telegram_update_inbox
GROUP BY status
ORDER BY status;
```

Inspect poison updates without changing them:

```sql
SELECT update_id, attempts, last_error, received_at, updated_at
FROM telegram_update_inbox
WHERE status = 'dead'
ORDER BY updated_at;
```

After fixing the underlying handler and reviewing the stored payload, requeue a
single dead update in a transaction:

```sql
BEGIN;
SELECT update_id, payload, last_error
FROM telegram_update_inbox
WHERE update_id = 123456789
FOR UPDATE;

UPDATE telegram_update_inbox
SET status = 'failed',
    attempts = 0,
    next_attempt_at = now(),
    lease_token = NULL,
    lease_until = NULL,
    last_error = NULL,
    updated_at = now()
WHERE update_id = 123456789
  AND status = 'dead';
COMMIT;
```

Never manually change a `processing` row while a bot replica may still own its
lease. Stop the bot first or wait until `lease_until` has passed.

On successful ACK, the payload is immediately replaced with an empty object;
the row and unique `update_id` remain as a dedupe receipt until
`TELEGRAM_UPDATE_RETENTION_DAYS`. This minimizes retention of message, contact,
and payment-related PII without reopening old updates for ingestion.
Dead-letter rows and their complete Telegram payloads use the deliberately shorter
`TELEGRAM_UPDATE_DEAD_RETENTION_DAYS` window. Review, export when required, or
requeue a dead update before that deadline. Pending, failed, and processing rows
are never removed by retention cleanup. Startup rejects a dead-letter retention
that is not shorter than processed retention.

## Webhook migration boundary

`TelegramUpdateService.ingest(..., source="webhook")` is transport-neutral and
already enforces the same `update_id` deduplication. A future HTTP webhook needs
only to:

- terminate HTTPS;
- validate Telegram's secret-token header and request size;
- parse a JSON object and call the shared ingestion service;
- return success only after the database transaction commits;
- leave handler execution to the existing inbox processor.

Do not run `getUpdates` and an installed webhook at the same time. The current
bot startup removes any webhook with `drop_pending_updates=False`, so that line
must become an explicit deployment-mode switch when the webhook HTTP adapter is
introduced.

## Rollback

The Alembic downgrade refuses to drop the inbox while any row is not
`processed`. Before a code-only rollback, stop polling first and let the inbox
drain. Prefer retaining the schema during an application rollback: dropping it
removes the only durable copy of updates Telegram may already consider
acknowledged.
