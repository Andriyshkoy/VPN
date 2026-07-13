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
4. Set `REDIS_PASSWORD` from a URL-safe generator such as
   `openssl rand -hex 32`. Production Compose injects that same value into the
   backend `REDIS_URL`; do not use unescaped `@`, `/`, `:`, or `%` characters.
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

The guarded remote script performs these gates before bot traffic:

1. Stops and verifies the absence of old writers.
2. Creates and validates both a cold volume backup (when PostgreSQL is stopped)
   and a fresh custom-format `pg_dump`.
3. Restores that dump into disposable PostgreSQL 16 and applies the exact
   digest-pinned migrations image.
4. Runs `alembic check`, accounting invariants, count/balance comparisons, and
   a live read-only Manager mTLS inventory test.
5. Migrates the live database and starts only the bot. Billing, payments,
   provisioning, notifications, worker, scheduler, admin, UI, and monitoring
   remain off during this canary.

Any failure after the schema upgrade stops the bot and preserves the upgraded
database for a fix-forward release; it never starts an incompatible old image
or automatically restores over newly accepted Telegram updates.

## Volumes and Compose identity

The file pins the project name to `vpn` and declares external volumes. Existing
production PostgreSQL must be exactly `vpn_db_data`; an alternate empty volume
is a hard stop. Create the new persistent Redis/Prometheus volumes once before
their first approved use:

```bash
docker volume inspect vpn_db_data
docker volume create vpn_redis_data
docker volume create vpn_prometheus_data
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

The sole current revision must be `f1a8c3d9e742`. Before starting any
application process, reconcile `user.balance` against `sum(ledger_entry.amount)`,
verify every user has one unique 32-character referral code, and review the
count and total of `referral_reward` rows created by the backfill. For the
initial rollout, perform the read-only Manager smoke test and start only the
bot canary, leaving admin, worker, and scheduler off:

```bash
docker compose --env-file .env --env-file release.env \
  -f docker-compose-prod.yml --profile bot run --rm --no-deps \
  --entrypoint python bot - < releases/RELEASE_SHA/manager_smoke.py
docker compose --env-file .env --env-file release.env \
  -f docker-compose-prod.yml --profile bot up -d --no-deps bot
```

Readiness requires PostgreSQL at the exact schema head, Redis, and valid Manager
TLS material. Run a read-only drift audit and review aggregated findings; keep
`VPN_DRIFT_REPAIR_ENABLED=false`.

After explicit approval, start components one at a time:

```bash
docker compose --env-file .env --env-file release.env \
  -f docker-compose-prod.yml --profile worker up -d --no-deps rq_worker
```

Observe the durable Telegram inbox, payment intents, VPN operations, ledger,
notification outbox, and queues before changing kill switches. Start
`rq_scheduler` last, using the `billing-scheduler` profile, only after billing
has been separately approved.

## Rollback

Prefer a code-only rollback while retaining schema head. After referral migration
`f1a8c3d9e742`, the fallback image must understand the non-null invite code and
referral accounting tables; pre-referral images cannot register users safely.
Older production code also writes balances without the immutable ledger and
must never run after migration `4a9f0d6c2e31` has been applied.

Stop bot/worker/scheduler first. Do not downgrade the database while any new
process is running. A database restore must use the post-key-rotation snapshot
or explicitly restore a matching Manager key generation without logging the
decrypted key.

Monitoring activation and exporter-role setup are documented in
[`observability.md`](observability.md). Monitoring is optional and is never a
dependency of a financial or VPN lifecycle commit.
