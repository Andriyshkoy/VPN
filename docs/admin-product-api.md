# Subscription and campaign administration

This change is prepared for review. It does not run a production migration,
enable billing, create campaigns, or send Telegram messages on startup.

## Subscriptions

The existing hourly billing engine remains in use. Basic costs **0.07 RUB per
hour per account**, includes Hysteria and limits it to one simultaneous client
session. Plus costs **0.14 RUB per hour per account** and includes Hysteria and
VLESS/REALITY with no session limit. The approximate monthly labels are 50 and
100 RUB; 720 hours actually cost 50.40 and 100.80 RUB. Issuance is free.

`subscriptions:read` allows the plan catalog, subscription list, per-user view
and migration preview. Owners and finance have `subscriptions:write`; other
staff can read. Plan changes require a reason, `Idempotency-Key`, and optionally
the displayed `expected_version`. They change the hourly plan immediately,
without charging or issuing new credentials. Incompatible old credentials are
staged for revocation by the existing durable operation workflow.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/admin/v1/subscriptions/plans` | Canonical hourly rates and plan limits |
| GET | `/api/admin/v1/subscriptions` | Page, optional `plan_code` and `status` filters |
| GET | `/api/admin/v1/users/{id}/subscription` | Current subscription or `null` |
| PATCH | `/api/admin/v1/users/{id}/subscription` | `{plan_code, reason, expected_version?}` |
| GET | `/api/admin/v1/subscriptions/migration/preview` | Read-only proposed mapping and counts |

Migration maps one active config to Basic and two or more to Plus. Fully
suspended accounts stay suspended. There is no HTTP migration-apply endpoint.
Migration preserves finite balances, the ledger and both manual account flags.

## Manual account exceptions

`PATCH /api/admin/v1/users/{id}/unlimited-balance` requires `balance:write`
(owner or finance). The body is `{enabled, expected_enabled, reason}`;
booleans must be JSON booleans. `Idempotency-Key` is required. The account keeps
its finite stored balance, displayed separately as `balance_display: "∞"`
while the flag is enabled. No ledger entry or artificial credit is created.

Audit and flag mutation commit in one transaction under the account row lock.
Reusing the same key with different input returns 409. Replaying an old request
returns the original result with `replayed: true` and does not undo a later
change. Neither known account IDs nor migration turn the flag on automatically.

The owner-only endpoint
`PATCH /api/admin/v1/users/{id}/business-metrics-exclusion` accepts the same
body. This independent flag joins configured exclusions in business analytics.
The full analytics scope, operational counts, stored wallet and source ledger
remain available. Unlimited balance alone does not remove past payments or
finite wallet liability; it removes the account from projected paid units.

## Reviewed Telegram campaigns

Campaign management is owner-only through the `campaigns:read`,
`campaigns:write` and `campaigns:send` permissions. Existing authenticated
sessions, CSRF checks and same-origin policy apply to every mutation.

| Method | Endpoint under `/api/admin/v1/campaigns` | Purpose |
| --- | --- | --- |
| GET / POST | empty path | List / create a draft |
| GET / PATCH | `/{id}` | Read / edit the current draft revision |
| POST | `/{id}/preview` | Review eligible count, skipped count and redacted samples |
| POST | `/{id}/launch` | Freeze the reviewed content and exact recipient set |
| POST | `/{id}/cancel` | Suppress remaining queued work |
| POST | `/{id}/retry` | Retry only definitive failed deliveries |

Drafts contain `{name, text, audience: {kind: "all" | "selected", user_ids: []}}`.
Selected IDs are internal database user IDs, not Telegram IDs. Messages are
plain text with Telegram's 4096 UTF-16-unit limit. Preview samples contain only
internal IDs and a redacted display label. Known blocked or deactivated users
are excluded.

A preview expires after 15 minutes. Launch requires its token, revision,
`expected_recipient_count`, `confirmation: "SEND"`, a reason and an
`Idempotency-Key`. Changes to text, audience or delivery eligibility invalidate
the preview. Recipient rows and outbox records are created atomically with the
audit event. Launch/retry keys remain durable across browser retries.

Campaign views, including list items and launch results, report the observed
`delivery_paused` worker setting.
When `NOTIFICATIONS_ENABLED=false`, launch only creates the durable queue;
the listener remains paused and messages are not sent. This switch is not
changed by campaign actions. Queued work waits until an independently approved
configuration change enables delivery.

The existing outbox and elected Telegram listener deliver at up to 20 campaign
messages per second. A pre-send database claim checks cancellation, recipient
eligibility and the immutable payload. Definitive Telegram rate limits preserve
their retry delay in PostgreSQL. Cancellation cannot recall an in-flight send.

Telegram has no client-supplied idempotency key. An interrupted or ambiguous
send therefore becomes `unknown` and is never retried automatically. A retry
operation does not resend `delivered`, `unknown`, `skipped` or cancelled rows.
Stale in-flight claims are reconciled to `unknown`; the reconciler never sends.
This favors avoiding duplicate broadcasts over silently promising exactly-once
delivery.
