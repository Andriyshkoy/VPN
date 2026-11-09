# Account subscriptions (prepared implementation)

This branch prepares the product migration. It does not deploy services, run a
production migration, enable billing, or send Telegram messages. Billing remains
disabled in the rollout configuration. Unlimited balance is **manual only** and
defaults to false for every existing and new account.

## Tariffs and hourly billing

| Code | Included services | Concurrent clients | Charge per UTC hour | Approximate monthly label | Actual 30 days |
|---|---|---|---:|---:|---:|
| `basic` | Hysteria | One authenticated QUIC client connection | 0.07 RUB | 50 RUB | 50.40 RUB |
| `plus` | Hysteria and VLESS/REALITY | Unlimited | 0.14 RUB | 100 RUB | 100.80 RUB |

The existing hourly `BillingService`, `BillingRun` and `periodic_charge` ledger
remain authoritative. A product account is charged once per eligible hour,
independent of its number of protocol credentials. Pending grants never accrue
charges. This is not a prepaid monthly
subscription; there is no paid-until date, monthly renewal or proration. Plan
changes take effect immediately for future hourly charges. A run with product
accounts must cover exactly one hour.

Credential issuance and reissue are free. New OpenVPN sales through the old
`create_paid_config` method are rejected. Historical paid operations retain their
retry and exact refund behavior. Unmigrated accounts retain the old billing
fallback; an account with a subscription is excluded from that fallback.

`User.unlimited_balance` bypasses service consumption debits. It preserves the
finite wallet and existing ledger, creates no synthetic credit or zero-value
entry, and does not change the selected plan. Explicit administrative wallet
adjustments remain finite and auditable. `exclude_from_business_metrics` is a
separate flag and does not grant free access.

## Core contract

`core.services.subscriptions.SubscriptionService(uow, *, clock=None,
config_service=None)` exposes:

| Method | Result and behavior |
|---|---|
| `catalog()` | Synchronous tuple of `PlanView` |
| `get_current(user_id)` | `SubscriptionView` or `None` |
| `get_by_tg_id(tg_id)` | Same view, resolves the owner on the server |
| `select_plan(user_id, plan_code, request_key=None, *, expected_version=None)` | Assigns immediately without charge or network calls; stages obsolete credential revocations |
| `issue(user_id, server_id=None, request_key=None)` | Issues included credentials or retries the existing durable intent; no fee |
| `regenerate(user_id, service_id, request_key)` | Replaces only that included service, with durable request idempotency |
| `suspend(user_id, request_key=None)` | Suspends current and retained legacy credentials |
| `resume(user_id, request_key=None)` | Resumes/creates current entitlements; positive finite wallet required only while billing is enabled |
| `export(user_id, service_id)` | Explicit `SubscriptionProfile` secret export; checks owner, current plan and active state |
| `prepare_entitlement(*, repos, user_id, desired_state, kind)` | Stages access transitions in the caller's transaction; suspension includes retained legacy keys |

Use `expected_version` to reject a stale plan-selection screen. The bot's
durable action journal and the admin's audited idempotency wrapper provide the
outer request replay protection. `select_plan` may use a transaction-bound UOW,
so its assignment and the admin audit commit together. Its remote revocations
are executed by the existing reconciler after commit. A changed active plan
returns to `pending` until its replacement entitlements are confirmed. Resuming
also keeps the account unbillable until every included service works; failed
preflight leaves its previous suspension intact. Successful remote operations
and the periodic pending reconciliation both finalize grants, so an interrupted
HTTP request does not strand the account.

`PlanView` contains `code`, `name`, `hourly_rate_rub`, `monthly_price_rub`,
`monthly_estimate_rub`, `currency`, `services`, and `max_sessions`. Monetary values
are `Decimal`; HTTP adapters serialize exact two-decimal strings.

`SubscriptionView` contains `id`, `user_id`, `plan_code`, `status`, `version`, the
same tariff money fields, `services`, `max_sessions`, `billing_paused`,
`unlimited_balance`, `next_charge_at`, and `accesses`. An access contains
`service_id`, `config_id`, `status`, `remote_access_id`, and `generation`.
Status is one of `pending`, `active`, `suspended`, `expired`, `cancelled`.
`expired` means hourly access was suspended after the finite wallet ran out;
it is distinct from a manual suspension. A top-up restores only compatible
mapped credentials of an expired account, never manually suspended accounts or
retired OpenVPN credentials.

Ordinary metadata never includes a credential. Only `export` returns a typed
`SubscriptionProfile(service_id, uri=None, format="uri", content=None)`;
both secret fields are excluded from its repr. VLESS uses `format="uri"` and
`uri`. Hysteria defaults to `format="singbox-json"` and a JSON string in
`content`, with `uri=null`. The payload contains one Hysteria outbound with
`tls.insecure=false` and `certificate_sha256: [base64(SHA256(leaf DER))]`.
This explicit format preserves server verification in Hiddify 5.5.5, whose raw
Hysteria URI parser ignores `pinSHA256`. The native URI remains available only
through the private node/config export's explicit `profile_format="native-uri"`
option; the bot and admin UI use the secure default.

`SubscriptionUnavailableError` is an `InvalidOperationError` with a safe
`reason_code`: `no_active_node` or `session_limit_unavailable`. Other invalid
states/plans use `InvalidOperationError`; missing owners use `UserNotFoundError`.

## Basic session enforcement and rotation

A Basic grant requires the live node catalog to advertise all of:

```json
{"max_sessions": true, "session_scope": "auth_id_per_server_instance", "enforcement": "atomic_quic_connection"}
```

Its persisted Hysteria policy contains `max_sessions: 1`. Plus explicitly uses
`max_sessions: null`. This limits client connections, not streams inside a
client. Enforcement is per auth identity and server instance; moving to multiple
nodes requires a separate global admission design.

Switching between unlimited and limited Hysteria policies creates a fresh
credential. A downgrade also revokes the previous VLESS access. Every new
provision intent records prior config identities, including retained legacy OpenVPN identities;
the executor refuses to claim
the replacement until the prior node bindings have durable revocation tombstones (or the legacy
Manager has confirmed a matching server/name revocation).
Node revocation must close active connections before success. Thus a failed
revoke cannot be bypassed by a later reconciliation run.

## Migration preparation

`SubscriptionMigrationService.preview()` is read only and returns a reviewable
manifest with owner, active count, target plan/status, old config IDs and a
snapshot hash. Active means both desired and actual state are `active`.

- One active config selects Basic; two or more select Plus. Both are assigned
  `pending` until credential cutover completes.
- No active configs but retained configs selects suspended Basic, without access.
- Accounts with only revoked/no configs are skipped.
- Existing subscription assignments are not silently overwritten.

`apply(manifest, source=...)` is deliberately absent from HTTP routes/startup.
It requires billing to be off, locks and rechecks the reviewed snapshot, and
records a durable migration source/hash. Repeating the same reviewed batch is
idempotent. It changes no money, flags, legacy credentials or remote services.
Issuing new credentials and retiring legacy access require the separately
reviewed rollout. Until then, pause/exhaustion also controls retained legacy
credentials, and top-ups never reactivate them. An unfinished pending cutover
blocks the hourly billing run before its claim or any balance movement. Direct
plan selection with retained legacy keys is rejected while billing is enabled;
operators must pause billing for that cutover.

Alembic revision `h6d0e3f4a5b6` adds schema only. Downgrade refuses to erase new
business history or enabled manual flags. No real user IDs or free-mode seeds
are embedded in the migration.
