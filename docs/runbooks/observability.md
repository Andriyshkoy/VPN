# VPN Hub alert runbooks

Before changing state, record the alert start time, current kill switches, the
Prometheus expression value, and relevant container logs. Do not retry billing
with an arbitrary period key and do not delete lifecycle or ledger rows.

## VPN Hub metrics target down

1. Check `docker compose ps admin statsd_exporter postgres_exporter redis_exporter prometheus`.
2. For the admin target, call `/health`, then `/ready` from inside the container.
3. For StatsD, inspect exporter logs and validate `statsd-mapping.yml`.
4. Restart only the failed monitoring component. Backend services do not depend
   on StatsD or Prometheus.
5. If admin itself is down, continue with the database/Redis checks below before
   restarting it.

For a PostgreSQL or Redis exporter target, first distinguish an exporter process
failure from a database failure. Inspect exporter logs without printing its
expanded connection environment. Restarting an exporter is safe; deleting the
database/Redis volume is not.

The guarded canary owns initial setup of the fixed `vpn_exporter` role and its
root-only `.env` credential. The role must differ from `POSTGRES_USER`, have
safe login attributes, and have exactly one direct role membership:
`pg_monitor`. Do not grant monitoring privileges to the application owner or
paste the exporter password into logs. A role-identity or membership failure is
a release blocker, not a reason to widen database grants.

## Redis unavailable

1. Set `BILLING_ENABLED=false` if worker retries or notification state are
   uncertain. Restart the affected backend processes so the switch is loaded.
2. Check Redis container health, disk space, AOF errors, and authentication/URL.
3. Do not delete Redis data to restore readiness. Restore from the approved
   backup if AOF recovery fails.
4. After Redis is healthy, verify RQ queues and notification processing before
   re-enabling billing.

## Billing stalled

1. Confirm `MAINTENANCE_MODE` and `BILLING_ENABLED` on every worker/scheduler.
2. Inspect scheduler and worker logs and the failed RQ registry.
3. Query `billing_run` for the most recent period and compare its exact
   `period_key`, start, and end with the expected UTC interval.
4. Never invent a new period key for a manual retry. Re-run the normal job only
   when the existing idempotent period identity is understood.
5. Keep billing disabled if database state and ledger totals disagree.

## Billing never completed

This alert waits for two configured billing intervals after the metrics process
starts, so an empty fresh database does not page immediately. Confirm that this
is an initialized production database, then check scheduler registration, worker
queues, kill switches, and `billing_run`. Do not manufacture a completed row to
clear the alert. Exercise the normal idempotent billing job only after its period
identity and ledger state are understood.

## Billing run stuck

Billing runs normally commit atomically, so a visible long-running row is
unexpected. Inspect database locks and worker logs. Do not mark the row completed
manually. Preserve the transaction/lock evidence, stop duplicate workers, and
use the same period identity for a controlled retry only after the cause is fixed.

## Background job errors

1. Filter by the `job` label (`billing`, `vpn_reconcile`, or
   `notification_outbox`).
2. Inspect the RQ failed registry and the corresponding worker traceback.
3. Follow the more specific billing, lifecycle, or notification runbook section.
4. Retry only after confirming the job's database idempotency key or operation
   lease prevents a duplicate side effect.

## VPN operation backlog

1. Keep billing decisions separate from Manager reachability; do not edit user
   balances to clear lifecycle work.
2. Group `vpn_operation` rows by status, kind, server, and `next_attempt_at`.
3. Check Manager health and control-plane connectivity for affected servers.
4. Verify there is no active unexpired lease before manually triggering
   reconciliation.
5. Allow the normal reconciler to converge once Manager service is restored.

## VPN operations exhausted

1. Inspect each exhausted operation, its immutable operation ID, attempts, target
   server, and last error.
2. Compare Manager reality with the config's desired and actual state.
3. Choose an explicit operator action: retry the same operation, supersede it
   with a newer desired state, or compensate a rejected provision reservation.
4. Never change only the status column or delete the operation; that removes the
   audit and fencing context.

## Notification outbox lag

1. Verify Redis readiness and that the dedicated outbox scheduler job exists.
2. Inspect `pending` rows by `created_at`/`next_attempt_at` and `queued` rows by
   `published_at`. A queued row older than the visibility timeout is retryable;
   a recent queued row may still be owned by the Telegram consumer.
3. Check RQ worker logs for Redis enqueue failures.
4. Re-run the publisher; stable notification IDs make enqueue idempotent.
5. Do not delete pending PostgreSQL outbox rows to reduce the alert.

## Notification outbox retrying

Follow the lag procedure, then inspect `attempts` and `last_error` without adding
the raw error text as a metric label. A repeated Telegram delivery failure belongs
to the downstream notification queue; a Redis publish failure belongs to this
PostgreSQL outbox.

## Telegram inbox backlog

1. Group `telegram_update_inbox` by bounded status and inspect the oldest
   non-terminal rows by `received_at`; do not paste payloads into tickets.
2. Check the bot processor task, database latency, expired leases, and handler
   timeouts before retrying anything.
3. A `processing` row with a live lease must not be manually replayed. Allow the
   lease owner to complete or expire.
4. Restore the processor and let normal fenced claims drain the backlog.

## Telegram inbox dead

Dead rows exhausted the configured processing attempts. Preserve their error and
lease history, inspect payloads only in the access-controlled database, and fix
the handler or dependency first. Requeue through an audited operator procedure;
never delete the row merely to clear the alert.

## Manager TLS material invalid

1. Keep provisioning disabled while readiness reports `manager_tls=false`.
2. Check that the configured CA, client certificate, and private key paths exist
   inside every Manager-using container and are mounted read-only.
3. Verify file permissions, certificate validity windows, and client key match
   without printing private-key contents.
4. Rotate files atomically in the host secret directory, restart one workload,
   and require `/ready` plus a controlled Manager request to pass before rolling
   the remaining workloads.

## Manager TLS certificate expiring

Identify the bounded `certificate=ca|client` label, issue the replacement through
the normal CA workflow, and validate it locally before rotation. Rotate well
before expiry; do not extend alert thresholds as a substitute for replacement.

## Manager error rate

1. Break down errors by `service`, `operation`, `outcome`, and bounded HTTP status.
2. Check whether a single Manager/server is responsible using structured logs;
   server identity is intentionally not a metric label.
3. For authentication failures, rotate or repair credentials through the normal
   server update procedure. Do not print API keys in logs or tickets.
4. For transport/5xx failures, pause provisioning if ambiguity grows and let
   durable operations retain their existing IDs for reconciliation.

## Manager latency

The histogram includes retry backoff, so first compare request retries and error
rate. Then check Manager CPU/disk/network and hub-to-Manager connectivity. Avoid
raising timeouts until the slow operation is known to be idempotent; longer
timeouts can increase concurrent leased work and delay reconciliation.

## Fleet server unreachable

Open the server in the admin panel and run one explicit health check. Verify the
private Hub-to-Manager route, mTLS, Manager service, and OpenVPN process without
changing user configs. Keep new placement closed until a fresh check succeeds;
durable lifecycle operations should be reconciled with their existing IDs.

## Fleet identity mismatch

Treat this as a possible endpoint replacement. Do not overwrite the stored
instance ID or activate placement merely to clear the alert. Confirm the host,
restore the intended Manager or complete a reviewed server replacement, then
run a fresh health/inventory check before accepting new configs.

## Fleet status stale

Run bounded health checks for active and draining servers from the fleet page.
If checks cannot remain fresh, investigate Manager reachability and the OpenVPN
status-file configuration. Do not mark status rows healthy by hand.

## Fleet server unhealthy

Inspect Manager readiness and OpenVPN data-plane status in the server detail.
Close new placement, repair the failed local PKI/file/service check, and require
a fresh successful check before reopening placement. Existing client configs
must not be reissued as part of this procedure.

## Fleet server certificate expiring

Identify the affected server from the fleet status page, renew its OpenVPN
server certificate through the existing PKI procedure, and test a separate
client connection. Preserve the CA and client profiles; do not rotate client
certificates merely because the server certificate is renewed.

## Fleet capacity exhausted

Check configured capacity, reserve, and non-revoked profile count. Either close
placement on the full node and activate a verified additional node, or raise the
limit only after checking real CPU/network/session headroom. Capacity changes
do not move or rewrite existing client configs.
