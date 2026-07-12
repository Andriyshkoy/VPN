# VPN Hub observability

The local stack exposes operational signals without adding a dependency to any
financial transaction or VPN lifecycle commit.

## Signal paths

- `admin:8000/metrics` renders durable gauges from PostgreSQL: billing runs,
  VPN-operation status/backlog/age, and notification-outbox status/lag/retries.
- `admin:8000/health` is a dependency-free liveness probe.
- `admin:8000/ready` requires PostgreSQL at the exact expected Alembic head and
  Redis. Maintenance mode is reported but does not make the API unready. When Manager TLS is enabled,
  configured CA/client files must also be readable, internally consistent, and
  within their certificate validity window.
- The PostgreSQL snapshot also reports Telegram inbox status, retryable backlog,
  oldest backlog age, and dead rows. Payloads and Telegram identifiers are never
  metric labels.
- Every backend process sends best-effort UDP StatsD observations for Manager
  requests and RQ jobs. `statsd_exporter` converts bounded DogStatsD tags into
  Prometheus counters and histograms.
- Prometheus scrapes the admin API and StatsD exporter every 15 seconds and
  evaluates `observability/alerts.yml`. PostgreSQL and Redis exporters provide
  engine-level capacity, persistence, connection, and failure signals.

StatsD loss must never fail a user operation. UDP counters can reset when the
exporter restarts; PostgreSQL-backed gauges remain authoritative for current
backlog and lag. No metric uses Telegram user ID, config name, operation ID,
server address, or raw exception text as a label.

## Local startup and verification

Start the backend monitoring path:

```bash
docker compose up --build -d db redis migrations admin rq_worker rq_scheduler statsd_exporter postgres_exporter redis_exporter prometheus
docker compose ps
curl -fsS http://127.0.0.1:19090/-/ready
```

The Prometheus UI is bound only to `127.0.0.1:19090`. Inspect its targets and
alerts at:

- `http://127.0.0.1:19090/targets`
- `http://127.0.0.1:19090/alerts`

## Production opt-in and network boundary

Monitoring is not part of the default production profile. Offline validation is
safe during maintenance and never starts a container:

```bash
OBSERVABILITY_ENABLED=true docker compose \
  -f docker-compose-prod.yml \
  --profile monitoring config --quiet
```

Runtime activation is a separate operator decision. After confirming the
desired admin/database/Redis services are already running, start only the four
monitoring containers:

```bash
OBSERVABILITY_ENABLED=true docker compose \
  -f docker-compose-prod.yml \
  --profile monitoring up -d \
  statsd_exporter postgres_exporter redis_exporter prometheus
```

Never run a generic `--profile monitoring up -d` during maintenance: Compose
would also start every unprofiled service, including the bot and scheduler. The
monitoring services intentionally have no `depends_on` links to Hub or data
services. The explicit service list starts only monitoring; unavailable targets
remain down in Prometheus until an operator separately starts them.

The monitoring images are pinned by release tag and multi-architecture digest.
Exporter credentials are interpolated from the deployment environment; no
database or Redis password is stored in this repository. Production Compose
requires `POSTGRES_EXPORTER_USER` and `POSTGRES_EXPORTER_PASSWORD` and has no
fallback to the application database account.

Bootstrap a dedicated read-only monitoring role once, using a generated password
from the deployment secret store (replace the database name if it is not `vpn`):

```sql
CREATE ROLE vpn_exporter WITH LOGIN PASSWORD '<generated-secret>';
GRANT CONNECT ON DATABASE vpn TO vpn_exporter;
GRANT pg_monitor TO vpn_exporter;
```

Store the role name/password only in the production environment, validate
`pg_up == 1`, and rotate with `ALTER ROLE vpn_exporter PASSWORD ...`. Do not grant
application schemas or table mutation privileges to this role.

`VPN_MANAGER_TLS_DIR_PROD` defaults production Manager TLS material to the host
directory `/etc/vpn-hub/manager-pki`. Local Compose separately uses
`VPN_MANAGER_TLS_DIR`, defaulting to `./secrets/vpn-manager`; copying the local
value cannot redirect the production bind. Both mount at
`/run/secrets/vpn-manager` read-only.

Production monitoring containers share an internal scrape network and the
private default Compose network needed to reach already-running targets; none
has a host port. Inspect readiness or query the Prometheus API with an operator-controlled
`docker compose exec prometheus` command; add a temporary loopback-only Compose
override only under the normal change procedure. The admin `/metrics`, `/health`,
and `/ready` routes are for container-network probes only; nginx has explicit
exact `404` locations for all three. Do not add them to the public `/api` proxy.

Probe the application from inside its container:

```bash
docker compose exec admin python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health').read().decode())"
docker compose exec admin python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/ready').read().decode())"
docker compose exec admin python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/metrics').read().decode()[:2000])"
```

Validate configuration using the pinned images before accepting changes:

```bash
docker run --rm \
  -v "$PWD/observability/statsd-mapping.yml:/etc/statsd-exporter/statsd-mapping.yml:ro" \
  prom/statsd-exporter:v0.29.0@sha256:632f705804922d50c1c95ba8ff9c8c0cc18d4bbb0cc265dc4f9ae708271c95b3 \
  --statsd.mapping-config=/etc/statsd-exporter/statsd-mapping.yml \
  --check-config
docker run --rm \
  -v "$PWD/observability:/etc/prometheus:ro" \
  --entrypoint promtool prom/prometheus:v3.12.0@sha256:69f5241418838263316593f7274a304b095c40bcf22e57272865da91bd60a8ac \
  check config /etc/prometheus/prometheus.yml
docker run --rm \
  -v "$PWD/observability:/etc/prometheus:ro" \
  --entrypoint promtool prom/prometheus:v3.12.0@sha256:69f5241418838263316593f7274a304b095c40bcf22e57272865da91bd60a8ac \
  check rules /etc/prometheus/alerts.yml
```

## Stable metric contract

Database-backed gauges:

- `vpn_hub_billing_runs{status}`
- `vpn_hub_billing_last_completed_timestamp_seconds`
- `vpn_hub_billing_oldest_running_age_seconds`
- `vpn_hub_vpn_operations{status}`
- `vpn_hub_vpn_operation_backlog`
- `vpn_hub_vpn_operation_oldest_backlog_age_seconds`
- `vpn_hub_notification_outbox{status}`
- `vpn_hub_notification_outbox_backlog`
- `vpn_hub_notification_outbox_oldest_backlog_age_seconds`
- `vpn_hub_notification_outbox_visibility_timeout_seconds`
- `vpn_hub_notification_outbox_retrying`
- `vpn_hub_telegram_update_inbox{status}`
- `vpn_hub_telegram_update_inbox_backlog`
- `vpn_hub_telegram_update_inbox_oldest_backlog_age_seconds`
- `vpn_hub_telegram_update_inbox_dead`
- `vpn_hub_dependency_ready{dependency}`
- `vpn_hub_feature_enabled{feature}`
- `vpn_hub_manager_tls_material_ready`
- `vpn_hub_manager_tls_certificate_expiry_timestamp_seconds{certificate}`

Cross-process counters and histograms:

- `vpn_hub_manager_requests_total{service,operation,method,outcome,status_code}`
- `vpn_hub_manager_retries_total{...}`
- `vpn_hub_manager_request_duration_seconds_bucket{...}`
- `vpn_hub_background_jobs_total{service,job,outcome}`
- `vpn_hub_background_job_duration_seconds_bucket{...}`
- `vpn_hub_notification_outbox_publish_total{service,outcome}`

Treat these names and label sets as an API. Add only bounded labels; never add a
per-user, per-config, per-operation, host, URL, or exception-message label.

## Staged rollout

1. Restore a recent database backup locally and run the full stack with
   `OBSERVABILITY_ENABLED=false`. Verify `/metrics`, `/ready`, and query cost.
2. Enable StatsD only in local Compose. Generate one Manager success and one
   controlled failure; verify both outcomes without changing billing switches.
3. Start the production `monitoring` profile with alerts visible but no external
   Alertmanager receiver. Verify all four scrape targets and observe normal
   values for at least two billing periods.
4. Route `severity=warning` to a non-paging channel. Tune lag and age thresholds
   from observed baselines.
5. Route `severity=page` only after every alert has an exercised runbook.

This repository intentionally supplies alert rules but no production
Alertmanager credentials or receiver. Configure receivers separately so secrets
never enter the repository.

## Disable and recovery

If StatsD causes unexpected overhead, set `OBSERVABILITY_ENABLED=false` and
restart backend containers. Health/readiness and PostgreSQL metrics continue to
work. Application services have no `depends_on` relationship with Prometheus or
StatsD and continue if either monitoring container is stopped.

To reset disposable local monitoring data without touching VPN Hub data:

```bash
docker compose stop prometheus statsd_exporter postgres_exporter redis_exporter
docker volume rm vpn_prometheus_data
docker compose up -d statsd_exporter postgres_exporter redis_exporter prometheus
```

Confirm the exact Compose project volume name with `docker volume ls` before
removing it. Never remove `db_data` or `redis_data` during this procedure.
For production, include `-f docker-compose-prod.yml --profile monitoring` in
these commands. Disabling the profile does not disable backend services.

Alert-specific diagnosis and recovery are in
[`docs/runbooks/observability.md`](runbooks/observability.md).
