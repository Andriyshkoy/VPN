# Production rollout and rollback

The production Compose file is deliberately fail-safe. A plain `docker compose
up` starts only PostgreSQL and Redis. Migrations, admin, bot, worker, scheduler,
UI, and monitoring require an explicit service/profile decision. Do not remove
these profile gates.

## Release prerequisites

1. Preserve and validate a database dump created after the current Manager API
   key rotation. Keep the earlier pre-rotation dump only as forensic history.
2. Set all six `VPN_*_IMAGE` values to immutable commit tags or registry
   digests. `unreleased` is a non-runnable guard; `latest` is forbidden.
3. Keep `MAINTENANCE_MODE=true`, `BILLING_ENABLED=false`,
   `PROVISIONING_ENABLED=false`, and `NOTIFICATIONS_ENABLED=false` for the
   initial canary.
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
   `root:10001 0640`, and the exact admin image running as `10001:10001` can
   complete a verified TLS handshake.
7. Confirm the active public certificate and `certbot.timer` before changing
   the application.

The root `Dockerfile` is the only backend build definition. Build all four
targets from one commit and never combine an app layer with a separately tagged
`vpn-base` image:

```bash
RELEASE=$(git rev-parse --short=12 HEAD)
for target in admin bot billing migrations; do
  docker build --pull --target "$target" -t "vpn-hub-${target}:${RELEASE}" .
done
```

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
docker compose -f docker-compose-prod.yml --profile hub \
  --profile bot --profile worker --profile billing-scheduler \
  --profile monitoring config --quiet
docker compose -f docker-compose-prod.yml config --images
```

Reject any backend/UI image ending in `:latest` or `:unreleased`.

## Staged startup

Start only the data services and prove the mounted database identity:

```bash
docker compose -f docker-compose-prod.yml up -d db redis
docker inspect vpn-db-1 --format '{{range .Mounts}}{{println .Destination .Name}}{{end}}'
```

The output for `/var/lib/postgresql/data` must be `vpn_db_data`. Check active
transactions and restore capacity before applying migrations.

Run the exact release migration container explicitly; do not rely on a reused
exited one-shot container:

```bash
docker compose -f docker-compose-prod.yml --profile hub run --rm --no-deps migrations
docker compose -f docker-compose-prod.yml --profile hub run --rm --no-deps \
  --entrypoint alembic migrations current
docker compose -f docker-compose-prod.yml --profile hub run --rm --no-deps \
  --entrypoint alembic migrations check
```

The sole current revision must be `c3a6f1e8b902`. Then start only the admin
canary, leaving bot, worker, and scheduler off:

```bash
docker compose -f docker-compose-prod.yml --profile hub up -d --no-deps admin
docker compose -f docker-compose-prod.yml --profile hub ps admin
```

Readiness requires PostgreSQL at the exact schema head, Redis, and valid Manager
TLS material. Run a read-only drift audit and review aggregated findings; keep
`VPN_DRIFT_REPAIR_ENABLED=false`.

After explicit approval, start components one at a time:

```bash
docker compose -f docker-compose-prod.yml --profile bot up -d --no-deps bot
docker compose -f docker-compose-prod.yml --profile worker up -d --no-deps rq_worker
```

Observe the durable Telegram inbox, payment intents, VPN operations, ledger,
notification outbox, and queues before changing kill switches. Start
`rq_scheduler` last, using the `billing-scheduler` profile, only after billing
has been separately approved.

## Rollback

Prefer a code-only rollback while retaining schema head. The minimum compatible
fallback is an immutable image based on commit `3a52f53`; older production code
writes balances without the immutable ledger and must never run after migration
`4a9f0d6c2e31` has been applied.

Stop bot/worker/scheduler first. Do not downgrade the database while any new
process is running. A database restore must use the post-key-rotation snapshot
or explicitly restore a matching Manager key generation without logging the
decrypted key.

Monitoring activation and exporter-role setup are documented in
[`observability.md`](observability.md). Monitoring is optional and is never a
dependency of a financial or VPN lifecycle commit.
