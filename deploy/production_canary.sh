#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

readonly POSTGRES_IMAGE="postgres:16@sha256:5a65324fe84dc41709ff914e90b07f3e2f577073ed27bf917d4873aca0c9ec51"
readonly EXPECTED_REVISION="f1a8c3d9e742"
readonly EXPECTED_PREVIOUS_REVISION="d78ffcb95ce5"

log() {
    printf '[deploy] %s\n' "$*"
}

die() {
    printf '[deploy] ERROR: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "missing command: $1"
}

env_value() {
    local key="$1"
    local file="$2"
    local count
    local line

    count="$(grep -Ec "^${key}=" "$file" || true)"
    [[ "$count" == "1" ]] || return 1
    line="$(grep -E "^${key}=" "$file")"
    [[ -n "${line#*=}" ]] || return 1
    printf '%s' "${line#*=}"
}

set_env_value() {
    local key="$1"
    local value="$2"
    local file="$3"
    local temporary

    temporary="$(mktemp "${file}.XXXXXX")"
    awk -v key="$key" -v value="$value" '
        BEGIN { replaced = 0 }
        index($0, key "=") == 1 {
            if (!replaced) {
                print key "=" value
                replaced = 1
            }
            next
        }
        { print }
        END {
            if (!replaced) {
                print key "=" value
            }
        }
    ' "$file" > "$temporary"
    chmod 0600 "$temporary"
    mv -f "$temporary" "$file"
}

compose_service_ids() {
    local service="$1"

    docker ps -q \
        --filter label=com.docker.compose.project=vpn \
        --filter "label=com.docker.compose.service=${service}"
}

compose_service_id() {
    local service="$1"
    local output
    local -a ids=()

    output="$(compose_service_ids "$service")" || return 1
    if [[ -n "$output" ]]; then
        mapfile -t ids <<< "$output"
    fi
    ((${#ids[@]} <= 1)) || return 2
    if ((${#ids[@]} == 1)); then
        printf '%s' "${ids[0]}"
    fi
}

stop_non_canary_containers() {
    local service
    local output
    local -a ids

    for service in \
        bot \
        rq_worker \
        rq_scheduler \
        admin \
        admin_frontend \
        nginx \
        statsd_exporter \
        postgres_exporter \
        redis_exporter \
        prometheus; do
        ids=()
        output="$(compose_service_ids "$service")" \
            || die "failed to enumerate ${service} containers"
        if [[ -n "$output" ]]; then
            mapfile -t ids <<< "$output"
        fi
        if ((${#ids[@]})); then
            log "stopping previous ${service} container"
            docker stop --time 60 "${ids[@]}" >/dev/null
        fi
    done
}

wait_healthy() {
    local container_id="$1"
    local label="$2"
    local state

    for _attempt in $(seq 1 45); do
        state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")"
        if [[ "$state" == "healthy" || "$state" == "running" ]]; then
            return 0
        fi
        if [[ "$state" == "exited" || "$state" == "dead" ]]; then
            die "${label} stopped during startup"
        fi
        sleep 2
    done
    die "${label} did not become healthy"
}

db_psql() {
    docker exec -i "$DB_CONTAINER_ID" sh -ec \
        'exec psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"' \
        sh "$@"
}

create_logical_backup() {
    local container_id="$1"
    local destination="$2"

    log "creating fresh logical production backup"
    docker exec "$container_id" sh -ec \
        'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-acl' \
        > "$destination"
    [[ -s "$destination" ]] || die "logical backup is empty"
    docker exec -i "$container_id" pg_restore --list \
        < "$destination" >/dev/null
    sha256sum "$destination" > "$destination.sha256"
    chmod 0600 "$destination" "$destination.sha256"
}

assert_no_database_clients() {
    local active_clients=""

    for _attempt in $(seq 1 10); do
        active_clients="$(db_psql -Atc "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() AND backend_type = 'client backend'")"
        [[ "$active_clients" == "0" ]] && return 0
        sleep 1
    done
    die "unexpected active database clients"
}

cleanup_preflight() {
    if [[ -n "${PREFLIGHT_CONTAINER:-}" ]]; then
        docker rm -f "$PREFLIGHT_CONTAINER" >/dev/null 2>&1 || true
    fi
    if [[ -n "${PREFLIGHT_NETWORK:-}" ]]; then
        docker network rm "$PREFLIGHT_NETWORK" >/dev/null 2>&1 || true
    fi
}

on_exit() {
    local exit_code=$?

    trap - EXIT
    cleanup_preflight
    if ((exit_code != 0)) && [[ "${LIVE_MIGRATION_STARTED:-false}" == "true" ]]; then
        local output
        local -a bot_ids=()

        if output="$(compose_service_ids bot)"; then
            if [[ -n "$output" ]]; then
                mapfile -t bot_ids <<< "$output"
                docker stop --time 30 "${bot_ids[@]}" >/dev/null 2>&1 || true
            fi
        else
            log "could not enumerate bot containers during failure cleanup"
        fi
        log "live schema may already be upgraded; old application images must not be restarted"
    fi
    exit "$exit_code"
}

main() {
[[ "$EUID" == "0" ]] || die "deployment must run as root"
: "${DEPLOY_ROOT:?DEPLOY_ROOT is required}"
: "${RELEASE_DIR:?RELEASE_DIR is required}"
: "${RELEASE_SHA:?RELEASE_SHA is required}"
[[ "$DEPLOY_ROOT" == /* ]] || die "DEPLOY_ROOT must be absolute"
[[ "$RELEASE_DIR" == "$DEPLOY_ROOT"/releases/* ]] || die "unexpected RELEASE_DIR"
[[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] || die "RELEASE_SHA must be a full commit SHA"

for command_name in awk docker flock grep openssl sha256sum tar; do
    require_command "$command_name"
done
docker compose version >/dev/null

exec 9>/var/lock/vpn-hub-deploy.lock
flock -n 9 || die "another production deployment is already running"

readonly APP_ENV="$DEPLOY_ROOT/.env"
readonly RELEASE_ENV="$RELEASE_DIR/release.env"
readonly COMPOSE_FILE="$RELEASE_DIR/docker-compose-prod.yml"
readonly PREFLIGHT_SQL="$RELEASE_DIR/preflight.sql"
readonly MANAGER_SMOKE="$RELEASE_DIR/manager_smoke.py"
readonly TELEGRAM_SMOKE="$RELEASE_DIR/telegram_smoke.py"
readonly RELEASE_OBSERVABILITY_DIR="$RELEASE_DIR/observability"

for required_file in \
    "$APP_ENV" \
    "$RELEASE_ENV" \
    "$COMPOSE_FILE" \
    "$PREFLIGHT_SQL" \
    "$MANAGER_SMOKE" \
    "$TELEGRAM_SMOKE" \
    "$RELEASE_OBSERVABILITY_DIR/alerts.yml" \
    "$RELEASE_OBSERVABILITY_DIR/prometheus.yml" \
    "$RELEASE_OBSERVABILITY_DIR/statsd-mapping.yml"; do
    [[ -s "$required_file" ]] || die "missing release file: $required_file"
done
chmod 0600 "$APP_ENV" "$RELEASE_ENV"

[[ "$(env_value RELEASE_SHA "$RELEASE_ENV")" == "$RELEASE_SHA" ]] \
    || die "release manifest SHA mismatch"
for key in POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD DATABASE_URL ENCRYPTION_KEY BOT_TOKEN; do
    env_value "$key" "$APP_ENV" >/dev/null \
        || die "production environment is missing ${key}"
done

declare -A expected_policy=(
    [MAINTENANCE_MODE]=true
    [BILLING_ENABLED]=false
    [PAYMENTS_ENABLED]=false
    [PROVISIONING_ENABLED]=false
    [NOTIFICATIONS_ENABLED]=false
    [REFERRAL_REWARDS_ENABLED]=true
    [REFERRAL_LEVEL_1_RATE_BPS]=500
    [REFERRAL_LEVEL_2_RATE_BPS]=100
    [REFERRAL_PROGRAM_VERSION]=v1-5pct-1pct
    [VPN_DRIFT_REPAIR_ENABLED]=false
    [OBSERVABILITY_ENABLED]=false
)
for key in "${!expected_policy[@]}"; do
    [[ "$(env_value "$key" "$RELEASE_ENV")" == "${expected_policy[$key]}" ]] \
        || die "unsafe release policy: ${key}"
done
[[ "$(env_value VPN_ENV_FILE "$RELEASE_ENV")" == "$APP_ENV" ]] \
    || die "release points at an unexpected application env file"
[[ "$(env_value VPN_MANAGER_TLS_DIR_PROD "$RELEASE_ENV")" == "/etc/vpn-hub/manager-pki" ]] \
    || die "release points at an unexpected Manager TLS directory"

declare -a image_keys=(
    VPN_ADMIN_IMAGE
    VPN_BOT_IMAGE
    VPN_BILLING_IMAGE
    VPN_MIGRATIONS_IMAGE
    VPN_ADMIN_FRONTEND_IMAGE
    VPN_NGINX_IMAGE
)
for key in "${image_keys[@]}"; do
    image_ref="$(env_value "$key" "$RELEASE_ENV")"
    [[ "$image_ref" =~ ^docker\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$ ]] \
        || die "${key} is not pinned by registry digest"
done

if systemctl is-active --quiet vpn.service 2>/dev/null; then
    die "legacy vpn.service is active"
fi
if systemctl is-enabled --quiet vpn.service 2>/dev/null; then
    die "legacy vpn.service is enabled"
fi
docker volume inspect vpn_db_data >/dev/null \
    || die "production database volume vpn_db_data is missing"
[[ "$(cat /var/lib/docker/volumes/vpn_db_data/_data/PG_VERSION)" == "16" ]] \
    || die "production database volume is not PostgreSQL 16"
[[ "$(df --output=avail -B1 "$DEPLOY_ROOT" | tail -n 1)" -gt 1073741824 ]] \
    || die "less than 1 GiB free on deployment filesystem"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
readonly TIMESTAMP
readonly BACKUP_DIR="$DEPLOY_ROOT/backups/${TIMESTAMP}-${RELEASE_SHA:0:12}"
install -d -m 0700 "$BACKUP_DIR"
cp -a "$APP_ENV" "$BACKUP_DIR/app.env.before"
if [[ -f "$DEPLOY_ROOT/docker-compose-prod.yml" ]]; then
    cp -a "$DEPLOY_ROOT/docker-compose-prod.yml" "$BACKUP_DIR/docker-compose-prod.yml.before"
fi
if [[ -f "$DEPLOY_ROOT/release.env" ]]; then
    cp -a "$DEPLOY_ROOT/release.env" "$BACKUP_DIR/release.env.before"
fi
if [[ -d "$DEPLOY_ROOT/observability" ]]; then
    cp -a "$DEPLOY_ROOT/observability" "$BACKUP_DIR/observability.before"
fi

stop_non_canary_containers
for service in \
    bot \
    rq_worker \
    rq_scheduler \
    admin \
    admin_frontend \
    nginx \
    statsd_exporter \
    postgres_exporter \
    redis_exporter \
    prometheus; do
    writer_id="$(compose_service_id "$service")" \
        || die "failed to enumerate ${service} containers after stop"
    [[ -z "$writer_id" ]] || die "writer container still running: ${service}"
done

readonly LOGICAL_DUMP="$BACKUP_DIR/hub-pre-release.dump"
DB_CONTAINER_ID="$(compose_service_id db)" \
    || die "failed to enumerate the production database container"
volume_consumers_output="$(docker ps -q --filter volume=vpn_db_data)" \
    || die "failed to enumerate vpn_db_data consumers"
volume_consumers=()
if [[ -n "$volume_consumers_output" ]]; then
    mapfile -t volume_consumers <<< "$volume_consumers_output"
fi

DB_WAS_RUNNING=false
if [[ -n "$DB_CONTAINER_ID" ]]; then
    DB_WAS_RUNNING=true
    if ((${#volume_consumers[@]} != 1)) \
        || [[ "${volume_consumers[0]}" != "$DB_CONTAINER_ID" ]]; then
        die "vpn_db_data has an unexpected running consumer"
    fi
    wait_healthy "$DB_CONTAINER_ID" PostgreSQL
    docker inspect -f '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}' "$DB_CONTAINER_ID" \
        | grep -Fx vpn_db_data >/dev/null \
        || die "PostgreSQL is not mounted on vpn_db_data"
    PRE_REVISION="$(db_psql -Atc 'SELECT version_num FROM alembic_version')"
    [[ "$PRE_REVISION" == "$EXPECTED_PREVIOUS_REVISION" || "$PRE_REVISION" == "$EXPECTED_REVISION" ]] \
        || die "unexpected source Alembic revision: ${PRE_REVISION}"
    PRE_USERS="$(db_psql -Atc 'SELECT count(*) FROM "user"')"
    PRE_CONFIGS="$(db_psql -Atc 'SELECT count(*) FROM vpn_config')"
    PRE_BALANCE="$(db_psql -Atc 'SELECT coalesce(sum(balance), 0)::numeric(18,2) FROM "user"')"
    assert_no_database_clients
    create_logical_backup "$DB_CONTAINER_ID" "$LOGICAL_DUMP"
else
    ((${#volume_consumers[@]} == 0)) \
        || die "vpn_db_data is mounted by an unmanaged running container"
    log "creating cold physical database-volume backup"
    tar --acls --xattrs --numeric-owner \
        -C /var/lib/docker/volumes/vpn_db_data/_data \
        -czf "$BACKUP_DIR/vpn_db_data.tar.gz" .
    tar -tzf "$BACKUP_DIR/vpn_db_data.tar.gz" >/dev/null
    sha256sum "$BACKUP_DIR/vpn_db_data.tar.gz" \
        > "$BACKUP_DIR/vpn_db_data.tar.gz.sha256"
fi

if ! env_value REDIS_PASSWORD "$APP_ENV" >/dev/null; then
    log "initializing production Redis credential"
    set_env_value REDIS_PASSWORD "$(openssl rand -hex 32)" "$APP_ENV"
fi
docker volume inspect vpn_redis_data >/dev/null 2>&1 \
    || docker volume create vpn_redis_data >/dev/null

readonly -a COMPOSE=(
    docker compose
    --env-file "$APP_ENV"
    --env-file "$RELEASE_ENV"
    -f "$COMPOSE_FILE"
)

"${COMPOSE[@]}" \
    --profile hub \
    --profile bot \
    --profile worker \
    --profile billing-scheduler \
    --profile monitoring \
    config --quiet
rendered_images="$("${COMPOSE[@]}" \
    --profile hub \
    --profile bot \
    --profile worker \
    --profile billing-scheduler \
    --profile monitoring \
    config --images)" || die "failed to enumerate rendered images"
if grep -Ev '@sha256:[0-9a-f]{64}$' <<< "$rendered_images"; then
    die "production image is not pinned by digest"
fi

log "pulling digest-pinned release images"
for key in "${image_keys[@]}"; do
    image_ref="$(env_value "$key" "$RELEASE_ENV")"
    docker pull "$image_ref" >/dev/null
    [[ "$(docker image inspect -f '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image_ref")" == "$RELEASE_SHA" ]] \
        || die "image revision label mismatch for ${key}"
done

log "starting production data services only"
"${COMPOSE[@]}" up -d db redis
DB_CONTAINER_ID="$(compose_service_id db)" \
    || die "failed to enumerate the reconciled database container"
REDIS_CONTAINER_ID="$(compose_service_id redis)" \
    || die "failed to enumerate the reconciled Redis container"
[[ -n "$DB_CONTAINER_ID" && -n "$REDIS_CONTAINER_ID" ]] \
    || die "data-service containers were not created"
wait_healthy "$DB_CONTAINER_ID" PostgreSQL
wait_healthy "$REDIS_CONTAINER_ID" Redis
docker inspect -f '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}' "$DB_CONTAINER_ID" \
    | grep -Fx vpn_db_data >/dev/null \
    || die "PostgreSQL is not mounted on vpn_db_data"

CURRENT_REVISION="$(db_psql -Atc 'SELECT version_num FROM alembic_version')"
[[ "$CURRENT_REVISION" == "$EXPECTED_PREVIOUS_REVISION" || "$CURRENT_REVISION" == "$EXPECTED_REVISION" ]] \
    || die "unexpected source Alembic revision: ${CURRENT_REVISION}"
CURRENT_USERS="$(db_psql -Atc 'SELECT count(*) FROM "user"')"
CURRENT_CONFIGS="$(db_psql -Atc 'SELECT count(*) FROM vpn_config')"
CURRENT_BALANCE="$(db_psql -Atc 'SELECT coalesce(sum(balance), 0)::numeric(18,2) FROM "user"')"
assert_no_database_clients
if [[ "$DB_WAS_RUNNING" == "true" ]]; then
    if [[ "$CURRENT_REVISION" != "$PRE_REVISION" ]] \
        || [[ "$CURRENT_USERS" != "$PRE_USERS" ]] \
        || [[ "$CURRENT_CONFIGS" != "$PRE_CONFIGS" ]] \
        || [[ "$CURRENT_BALANCE" != "$PRE_BALANCE" ]]; then
        die "database changed while data services were reconciled"
    fi
else
    PRE_REVISION="$CURRENT_REVISION"
    PRE_USERS="$CURRENT_USERS"
    PRE_CONFIGS="$CURRENT_CONFIGS"
    PRE_BALANCE="$CURRENT_BALANCE"
    create_logical_backup "$DB_CONTAINER_ID" "$LOGICAL_DUMP"
fi

MIGRATIONS_IMAGE="$(env_value VPN_MIGRATIONS_IMAGE "$RELEASE_ENV")"
FERNET_KEY="$(env_value ENCRYPTION_KEY "$APP_ENV")"
PREFLIGHT_NETWORK="vpn-preflight-${RELEASE_SHA:0:12}-$$"
PREFLIGHT_CONTAINER="vpn-preflight-pg-${RELEASE_SHA:0:12}-$$"
docker network create "$PREFLIGHT_NETWORK" >/dev/null
docker run -d \
    --name "$PREFLIGHT_CONTAINER" \
    --network "$PREFLIGHT_NETWORK" \
    --tmpfs /var/lib/postgresql/data:rw,noexec,nosuid,size=512m \
    -v "$BACKUP_DIR:/backup:ro" \
    -e POSTGRES_DB=vpn \
    -e POSTGRES_USER=vpn \
    -e POSTGRES_PASSWORD=vpn \
    --health-cmd 'pg_isready -U vpn -d vpn' \
    --health-interval 2s \
    --health-timeout 2s \
    --health-retries 30 \
    "$POSTGRES_IMAGE" >/dev/null
wait_healthy "$PREFLIGHT_CONTAINER" preflight-PostgreSQL
docker exec "$PREFLIGHT_CONTAINER" pg_restore \
    -U vpn -d vpn --no-owner --no-privileges --exit-on-error \
    /backup/hub-pre-release.dump

log "testing exact migration image against restored production backup"
docker run --rm \
    --network "$PREFLIGHT_NETWORK" \
    -e DATABASE_URL="postgresql+asyncpg://vpn:vpn@${PREFLIGHT_CONTAINER}:5432/vpn" \
    -e ENCRYPTION_KEY="$FERNET_KEY" \
    "$MIGRATIONS_IMAGE"
docker run --rm \
    --network "$PREFLIGHT_NETWORK" \
    --entrypoint alembic \
    -e DATABASE_URL="postgresql+asyncpg://vpn:vpn@${PREFLIGHT_CONTAINER}:5432/vpn" \
    -e ENCRYPTION_KEY="$FERNET_KEY" \
    "$MIGRATIONS_IMAGE" check
docker exec -i "$PREFLIGHT_CONTAINER" psql \
    -v ON_ERROR_STOP=1 -U vpn -d vpn < "$PREFLIGHT_SQL"
[[ "$(docker exec "$PREFLIGHT_CONTAINER" psql -U vpn -d vpn -Atc 'SELECT count(*) FROM "user"')" == "$PRE_USERS" ]] \
    || die "restored migration changed user count"
[[ "$(docker exec "$PREFLIGHT_CONTAINER" psql -U vpn -d vpn -Atc 'SELECT count(*) FROM vpn_config')" == "$PRE_CONFIGS" ]] \
    || die "restored migration changed config count"
[[ "$(docker exec "$PREFLIGHT_CONTAINER" psql -U vpn -d vpn -Atc 'SELECT coalesce(sum(balance), 0)::numeric(18,2) FROM "user"')" == "$PRE_BALANCE" ]] \
    || die "restored migration changed aggregate balance"

BOT_IMAGE="$(env_value VPN_BOT_IMAGE "$RELEASE_ENV")"
log "testing exact bot image against restored data and live Manager mTLS"
PREFLIGHT_MANAGER_LOG="$BACKUP_DIR/preflight-manager-smoke.log"
: > "$PREFLIGHT_MANAGER_LOG"
chmod 0600 "$PREFLIGHT_MANAGER_LOG"
if ! docker run --rm -i \
    --network "$PREFLIGHT_NETWORK" \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --tmpfs /tmp \
    --env-file "$APP_ENV" \
    -e DATABASE_URL="postgresql+asyncpg://vpn:vpn@${PREFLIGHT_CONTAINER}:5432/vpn" \
    -e VPN_MANAGER_TLS_ENABLED=true \
    -e VPN_MANAGER_MTLS_REQUIRED=true \
    -e VPN_MANAGER_TLS_PORT=16291 \
    -e VPN_MANAGER_CA_CERT_PATH=/run/secrets/vpn-manager/ca.crt \
    -e VPN_MANAGER_CLIENT_CERT_PATH=/run/secrets/vpn-manager/client.crt \
    -e VPN_MANAGER_CLIENT_KEY_PATH=/run/secrets/vpn-manager/client.key \
    -v /etc/vpn-hub/manager-pki:/run/secrets/vpn-manager:ro \
    --entrypoint python \
    "$BOT_IMAGE" - < "$MANAGER_SMOKE" \
    > "$PREFLIGHT_MANAGER_LOG" 2>&1; then
    die "preflight Manager mTLS smoke failed; details retained in the private backup directory"
fi
log "preflight Manager mTLS smoke passed"
log "testing production Telegram identity before live migration"
PREFLIGHT_TELEGRAM_LOG="$BACKUP_DIR/preflight-telegram-smoke.log"
: > "$PREFLIGHT_TELEGRAM_LOG"
chmod 0600 "$PREFLIGHT_TELEGRAM_LOG"
if ! docker run --rm -i \
    --network "$PREFLIGHT_NETWORK" \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --tmpfs /tmp \
    --env-file "$APP_ENV" \
    --entrypoint python \
    "$BOT_IMAGE" - < "$TELEGRAM_SMOKE" \
    > "$PREFLIGHT_TELEGRAM_LOG" 2>&1; then
    die "preflight Telegram smoke failed; details retained in the private backup directory"
fi
log "preflight Telegram smoke passed"
cleanup_preflight
PREFLIGHT_CONTAINER=""
PREFLIGHT_NETWORK=""

LIVE_MIGRATION_STARTED=true
log "applying migration to production"
"${COMPOSE[@]}" --profile bot run --rm --no-deps migrations
"${COMPOSE[@]}" --profile bot run --rm --no-deps \
    --entrypoint alembic migrations current
"${COMPOSE[@]}" --profile bot run --rm --no-deps \
    --entrypoint alembic migrations check
db_psql < "$PREFLIGHT_SQL"
[[ "$(db_psql -Atc 'SELECT count(*) FROM "user"')" == "$PRE_USERS" ]] \
    || die "production migration changed user count"
[[ "$(db_psql -Atc 'SELECT count(*) FROM vpn_config')" == "$PRE_CONFIGS" ]] \
    || die "production migration changed config count"
[[ "$(db_psql -Atc 'SELECT coalesce(sum(balance), 0)::numeric(18,2) FROM "user"')" == "$PRE_BALANCE" ]] \
    || die "production migration changed aggregate balance"

log "verifying live Manager mTLS inventory read"
LIVE_MANAGER_LOG="$BACKUP_DIR/live-manager-smoke.log"
: > "$LIVE_MANAGER_LOG"
chmod 0600 "$LIVE_MANAGER_LOG"
if ! "${COMPOSE[@]}" --profile bot run --rm --no-deps \
    --entrypoint python bot - < "$MANAGER_SMOKE" \
    > "$LIVE_MANAGER_LOG" 2>&1; then
    die "live Manager mTLS smoke failed; details retained in the private backup directory"
fi
log "live Manager mTLS smoke passed"

log "starting only the Telegram bot canary"
BOT_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
"${COMPOSE[@]}" --profile bot up -d --no-deps bot
BOT_CONTAINER_ID="$(compose_service_id bot)"
[[ -n "$BOT_CONTAINER_ID" ]] || die "bot container was not created"
for _attempt in $(seq 1 35); do
    [[ "$(docker inspect -f '{{.State.Running}}' "$BOT_CONTAINER_ID")" == "true" ]] \
        || die "bot stopped during canary observation"
    sleep 2
done
CANARY_LOG="$BACKUP_DIR/bot-canary.log"
docker logs --since "$BOT_STARTED_AT" "$BOT_CONTAINER_ID" \
    > "$CANARY_LOG" 2>&1 || die "failed to read bot canary logs"
chmod 0600 "$CANARY_LOG"
if grep -Eqi 'Unauthorized|Conflict: terminated by other getUpdates|Traceback|CRITICAL' "$CANARY_LOG"; then
    mv "$CANARY_LOG" "$BACKUP_DIR/bot-canary-error.log"
    die "bot canary log contains a fatal Telegram/runtime error"
fi
rm -f "$CANARY_LOG"
LIVE_TELEGRAM_LOG="$BACKUP_DIR/live-telegram-smoke.log"
: > "$LIVE_TELEGRAM_LOG"
chmod 0600 "$LIVE_TELEGRAM_LOG"
if ! "${COMPOSE[@]}" --profile bot exec -T bot python - < "$TELEGRAM_SMOKE" \
    > "$LIVE_TELEGRAM_LOG" 2>&1; then
    die "live Telegram smoke failed; details retained in the private backup directory"
fi
log "live Telegram smoke passed"

install -m 0600 "$COMPOSE_FILE" "$DEPLOY_ROOT/docker-compose-prod.yml"
install -m 0600 "$RELEASE_ENV" "$DEPLOY_ROOT/release.env"
install -d -m 0755 "$DEPLOY_ROOT/observability"
install -m 0644 \
    "$RELEASE_OBSERVABILITY_DIR/alerts.yml" \
    "$RELEASE_OBSERVABILITY_DIR/prometheus.yml" \
    "$RELEASE_OBSERVABILITY_DIR/statsd-mapping.yml" \
    "$DEPLOY_ROOT/observability/"
printf '%s\n' "$RELEASE_SHA" > "$DEPLOY_ROOT/current-release"
chmod 0600 "$DEPLOY_ROOT/current-release"
LIVE_MIGRATION_STARTED=false

log "deployment complete: bot only; billing, new payments, and provisioning remain disabled"
log "backup directory: ${BACKUP_DIR}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    trap on_exit EXIT
    main "$@"
fi
