# Production rollout and rollback

The production Compose file is deliberately fail-safe. A plain `docker compose
up` starts only PostgreSQL and Redis. Migrations, admin, bot, worker, scheduler,
UI, and monitoring require an explicit service/profile decision. Do not remove
these profile gates.

## Release prerequisites

1. Preserve and validate a database dump created after the current Manager API
   key rotation. Keep the earlier pre-rotation dump only as forensic history.
2. Set all six `VPN_*_IMAGE` values to registry digests. Production Compose
   refuses to render when any image variable is missing; `latest`,
   `unreleased`, and tag-only releases are forbidden.
3. Keep `MAINTENANCE_MODE=true`, `BILLING_ENABLED=false`,
   `PAYMENTS_ENABLED=false`, `PROVISIONING_ENABLED=false`, and
   `NOTIFICATIONS_ENABLED=false` for the initial canary. Successful-payment
   updates for invoices captured before the switch remain creditable.
4. Preserve any existing `REDIS_PASSWORD`. On first guarded deployment the
   canary generates a URL-safe value with `openssl rand -hex 32` when it is
   absent and persists it in the root-only production `.env`.
5. Require the Manager control plane:

   ```dotenv
   VPN_MANAGER_TLS_ENABLED=true
   VPN_MANAGER_MTLS_REQUIRED=true
   VPN_MANAGER_TLS_PORT=16291
   VPN_MANAGER_TLS_DIR_PROD=/etc/vpn-hub/manager-pki
   VPN_DRIFT_REPAIR_ENABLED=false
   ```

6. Verify the mTLS directory is `root:10001 0750`, the client key is
   `root:10001 0640`, and the exact bot image running as `10001:10001` can
   complete a verified TLS inventory read.
7. Confirm the active public certificate and `certbot.timer` before changing
   the application.
8. Pin the referral policy explicitly in the production environment:

   ```dotenv
   REFERRAL_REWARDS_ENABLED=true
   REFERRAL_LEVEL_1_RATE_BPS=500
   REFERRAL_LEVEL_2_RATE_BPS=100
   REFERRAL_PROGRAM_VERSION=v1-5pct-1pct
   ```

   Export the count and total amount of `credited` `provider_payment` rows
   before migration. Those rows are the complete and only authoritative input
   to the historical referral backfill.

The root `Dockerfile` is the only backend build definition. Build all four
targets from one commit and never combine an app layer with a separately tagged
`vpn-base` image:

```bash
RELEASE=$(git rev-parse --short=12 HEAD)
for target in admin bot billing migrations; do
  docker build --pull --target "$target" -t "vpn-hub-${target}:${RELEASE}" .
done
```

## GitHub Actions release path

`.github/workflows/ci.yml` runs on every pull request and `main` push. It checks
backend formatting, the complete unit/concurrency suite against PostgreSQL,
fresh Alembic upgrades and autogenerate drift, frontend lint/build, production
Compose rendering, and all six Docker build targets. Actions are pinned by
commit SHA and PR jobs receive no production secrets.

`.github/workflows/release.yml` is manual-only and accepts only the current
`main` revision plus the exact confirmation `DEPLOY_BOT_CANARY`. It publishes
all images with a full commit tag, records the registry digests, enters the
serialized `production` environment, and uploads only reviewed deployment
files. Application secrets, the Telegram token, the Fernet key, and database
credentials remain solely in `/opt/vpn/.env` on the server.

Set the optional repository variable `ADMIN_PUBLIC_ORIGIN` to the path-free
HTTPS origin served by the host proxy. It defaults to
`https://admin.vpn.andriyshkoy.ru`. Every mutating workflow rechecks that its SHA
is still current `main` and has the required exact successful predecessor runs
after production approval, immediately before preparing SSH access.

The guarded remote script performs these gates before bot traffic:

1. Stops and verifies the absence of old writers.
2. Creates and validates both a cold volume backup (when PostgreSQL is stopped)
   and a fresh custom-format `pg_dump`.
3. Restores that dump into disposable PostgreSQL 16 and applies the exact
   digest-pinned migrations image.
4. Runs `alembic check`, accounting invariants, count/balance comparisons, and
   authenticated Manager `/status` plus inventory checks. Every Manager must
   report ready, an `up` OpenVPN data plane, and a unique/matching instance ID.
5. Creates the external Redis/Prometheus volumes when absent and reconciles a
   dedicated `vpn_exporter` login with only `pg_monitor` membership. The
   exporter credential is generated into the root-only `.env` and verified by
   a real database login; the application database owner is never reused.
6. Migrates the live database and starts only the bot. Billing, payments,
   provisioning, notifications, worker, scheduler, admin, UI, and monitoring
   remain off during this canary.

Any failure after the schema upgrade stops the bot and preserves the upgraded
database for a fix-forward release; it never starts an incompatible old image
or automatically restores over newly accepted Telegram updates.

## Volumes and Compose identity

The file pins the project name to `vpn` and declares external volumes. Existing
production PostgreSQL must be exactly `vpn_db_data`; an alternate empty volume
is a hard stop. The guarded canary creates the new Redis/Prometheus volumes on
first use and verifies them again before activation. Operators can inspect all
three identities without changing them:

```bash
docker volume inspect vpn_db_data
docker volume inspect vpn_redis_data
docker volume inspect vpn_prometheus_data
```

Never run `docker compose down -v`. External volumes are intentional protection
against project-name and teardown mistakes.

Render and inspect the release without starting anything:

```bash
docker compose --env-file .env --env-file release.env \
  -f docker-compose-prod.yml --profile hub \
  --profile bot --profile worker --profile billing-scheduler \
  --profile monitoring config --quiet
docker compose --env-file .env --env-file release.env \
  -f docker-compose-prod.yml config --images
```

Reject any backend/UI image ending in `:latest` or `:unreleased`.

The production admin proxy binds only to `127.0.0.1:14081`. The host Nginx
must terminate TLS for `admin.vpn.andriyshkoy.ru` and proxy to that loopback
address. Do not publish port `14081` on a public interface: doing so would
bypass the host TLS boundary.

## Staged startup

Start only the data services and prove the mounted database identity:

```bash
docker compose --env-file .env --env-file release.env \
  -f docker-compose-prod.yml up -d db redis
docker inspect vpn-db-1 --format '{{range .Mounts}}{{println .Destination .Name}}{{end}}'
```

The output for `/var/lib/postgresql/data` must be `vpn_db_data`. Check active
transactions and restore capacity before applying migrations.

Run the exact release migration container explicitly; do not rely on a reused
exited one-shot container:

```bash
docker compose --env-file .env --env-file release.env \
  -f docker-compose-prod.yml --profile bot run --rm --no-deps migrations
docker compose --env-file .env --env-file release.env \
  -f docker-compose-prod.yml --profile bot run --rm --no-deps \
  --entrypoint alembic migrations current
docker compose --env-file .env --env-file release.env \
  -f docker-compose-prod.yml --profile bot run --rm --no-deps \
  --entrypoint alembic migrations check
```

The sole current revision must be `e9f1a2b3c4d5`. Before starting any
application process, reconcile `user.balance` against `sum(ledger_entry.amount)`,
verify every user has one unique 32-character referral code, and confirm that
the admin/fleet migrations did not change user, configuration, or aggregate
balance counts. The Telegram action journal intentionally has no historical
backfill because processed inbox payloads have already been erased. Bot actions
start accumulating after this migration; existing finance, referral, VPN,
account, and admin events appear in the unified timeline immediately. Perform
the read-only Manager smoke test and start only the bot canary, leaving admin,
worker, and scheduler off:

```bash
docker compose --env-file .env --env-file release.env \
  -f docker-compose-prod.yml --profile bot run --rm --no-deps \
  --entrypoint python bot - < releases/RELEASE_SHA/manager_smoke.py
docker compose --env-file .env --env-file release.env \
  -f docker-compose-prod.yml --profile bot up -d --no-deps bot
```

After the bot canary is healthy, activate the admin control plane with the
separate `Activate admin hub` workflow. It rechecks the exact release SHA,
schema head, bot health, loopback HTTP contract and the configured public HTTPS
origin. Public acceptance verifies the normal certificate/SNI path, the SPA,
and the same-origin unauthenticated API boundary before committing the marker.
It then starts only admin, frontend, proxy and monitoring; it does not enable
billing, payments, provisioning, notifications, worker, or scheduler.

Readiness requires PostgreSQL at the exact schema head, Redis, and valid Manager
TLS material. Run a read-only drift audit and review aggregated findings; keep
`VPN_DRIFT_REPAIR_ENABLED=false`.

## Full production promotion

Run the manual `Promote full production` workflow with the exact confirmation
`PROMOTE_FULL_PRODUCTION`. It accepts only the current `main` SHA and requires
successful `CI`, `Release bot canary`, and `Activate admin hub` runs for that
same SHA. The complete gate is repeated after the protected `production`
environment approval; no image is rebuilt or uploaded at this stage.

On the host, `promote_full_production.sh` acquires the same deployment lock and
requires both `current-release` and `current-admin-hub` to equal the requested
full SHA. It also requires the immutable staged manifest to remain fail-closed,
the database to be at `e9f1a2b3c4d5`, all staged images and running canary
containers to match their registry digests and revision labels, and both
Manager and Telegram read-only smokes to pass.

The script copies the active root-only `release.env` into a timestamped backup,
then atomically installs a runtime manifest with this policy:

```dotenv
MAINTENANCE_MODE=false
BILLING_ENABLED=true
PAYMENTS_ENABLED=true
PROVISIONING_ENABLED=true
NOTIFICATIONS_ENABLED=false
REFERRAL_REWARDS_ENABLED=true
VPN_DRIFT_REPAIR_ENABLED=false
OBSERVABILITY_ENABLED=true
```

Admin is recreated and accepted first. A force-recreated RQ worker then starts
before the bot is recreated and its ingress reopens. This happens only after
public HTTPS, accounting, Manager mTLS and Telegram identity checks pass.
`rq_scheduler` is force-recreated as the final service start. The script writes
`current-production` only after every intended service is running the exact
staged image and has loaded the promoted switches.

Observe the durable Telegram inbox, payment intents, VPN operations, ledger,
notification outbox, RQ registries and billing periods immediately after
promotion. Existing queued notifications remain paused; due idempotent billing
periods can run as soon as the worker and scheduler are enabled.

## Rollback

Prefer a code-only rollback while retaining schema head. After Telegram action
audit migration `e9f1a2b3c4d5`, keep the admin control plane stopped unless its image
understands database sessions, audit immutability and fleet lifecycle fields.
Any bot fallback must still understand referral migration `f1a8c3d9e742`;
pre-referral images cannot register users safely. Older production code also
writes balances without the immutable ledger and must never run after migration
`4a9f0d6c2e31` has been applied.

Stop bot/worker/scheduler first. Do not downgrade the database while any new
process is running. A database restore must use the post-key-rotation snapshot
or explicitly restore a matching Manager key generation without logging the
decrypted key.

If full promotion fails before it is committed, the guarded script first stops
scheduler, worker, bot and admin, atomically restores the backed-up fail-closed
`release.env`, removes a matching partial `current-production` marker, and only
then recreates admin and bot under that policy. It deliberately
does not attempt to reverse already committed Telegram, payment, ledger, or VPN
side effects. An unclean host interruption leaves `promotion-in-progress`; do
not delete it until the running containers and the active `release.env` have
been inspected against the referenced backup.

Monitoring activation and exporter-role setup are documented in
[`observability.md`](observability.md). Monitoring is optional and is never a
dependency of a financial or VPN lifecycle commit.
