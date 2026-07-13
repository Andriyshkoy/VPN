# Invite-only access and referral accounting

## Program policy

- Level 1 receives 5% of each confirmed provider deposit.
- Level 2 receives 1% of the same deposit.
- Rewards are non-withdrawable VPN balance and stop after two levels.
- Manual/admin top-ups, opening balances, refunds, and referral rewards never
  generate another reward.
- Attribution is fixed when a new user follows a valid opaque invite. Existing
  accounts are grandfathered and can never be rebound by another link.
- Amounts are rounded to kopecks with `ROUND_HALF_UP`; a zero result is skipped.

This application release supports exactly policy `v1-5pct-1pct`. Startup
rejects an in-place environment change to its version or rates, so a delayed
payment cannot silently receive different economics. A future policy change
requires a new versioned code and migration release.

`referral_reward` snapshots the source payment, payer, beneficiary, level,
rate, source amount, reward amount, currency, ledger entry, and policy version.
Both the audit row and the referenced ledger row are immutable. For an enabled
live capture, payer credit and all eligible referral credits
commit in one database transaction, with unique payment/level and ledger
idempotency guards. A delayed catch-up commits its reward rows and balances
atomically against the already credited payment.

The billing scheduler also runs an idempotent catch-up every five minutes. It
settles at most 100 credited provider payments per run, so a rolling deployment
or a temporary referral kill switch cannot permanently lose an eligible reward.
The job is skipped in maintenance mode and while
`REFERRAL_REWARDS_ENABLED=false`; its success, error, and skipped outcomes use
the standard `background_jobs` and `background_job_duration` metrics with
`job=referral_reconcile`. Any VPN unsuspension is stored as a durable operation;
the regular VPN reconciliation job performs the remote Manager call, so a large
financial backlog cannot monopolize the billing queue with network requests.
If immutable payment/reward accounting is contradictory, the savepoint rolls
back any partial reward and marks that payment `invalid_accounting`. The error
is logged for operator review while newer payments continue through the queue.
After committing the batch, the job reports an error outcome so the existing
background-job alert fires; the next scheduled pass is no longer blocked by the
quarantined row.

## Access boundary

Every existing `user` row may continue to use the bot. An unknown Telegram
account is admitted only through a private deep link whose payload is
`ref_<32 URL-safe characters>`. Numeric Telegram IDs and malformed/unknown
codes are rejected without creating a row or sending a message.

The outer Telegram middleware covers messages, callbacks, and pre-checkout
queries. Unknown callbacks receive an empty acknowledgement to close Telegram's
spinner. Unknown pre-checkout queries receive `ok=false`. A captured payment is
never silently discarded: it must resolve to an existing account or remain a
retryable reconciliation error.

## Historical backfill

Migration `f1a8c3d9e742` calculates 5%/1% for every RUB
`provider_payment.status='credited'` row with an accounting ledger reference.
It is deterministic and idempotent: matching reward rows are retained, while a
partial or contradictory accounting record aborts the migration.

The legacy production schema did not journal provider payments. Its
`opening_balance` rows represent only an aggregate at migration time and can
include deposits, administrative credits, and already-consumed service. They
must not be treated as historical deposits. Importing older payments requires
a provider export containing a stable charge ID, Telegram user/payment owner,
amount, currency, and capture time; reconcile that manifest separately before
issuing any additional credit.

## Manual refunds

Automated refund/chargeback settlement is intentionally out of scope. When a
provider payment is refunded manually, withdraw the original credited amount
from the payer and locate its immutable `referral_reward` rows to withdraw the
corresponding amounts from each beneficiary. Use separate stable idempotency
keys through the admin balance operation, and keep the provider refund
reference in the operator record. The admin withdrawal deliberately refuses a
negative balance; if a credited amount has already been spent, record and
resolve that exceptional debt manually instead of deleting or editing immutable
reward/ledger rows.
