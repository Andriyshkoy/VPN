#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

readonly EXPECTED_REVISION="e9f1a2b3c4d5"
readonly POSTGRES_EXPORTER_ROLE="vpn_exporter"
readonly -a HUB_SERVICES=(admin admin_frontend nginx)
readonly -a MONITORING_SERVICES=(
    statsd_exporter
    postgres_exporter
    redis_exporter
    prometheus
)

APP_ENV=""
RELEASE_ENV=""
COMPOSE_FILE=""
ADMIN_PUBLIC_ORIGIN=""
ACTIVATION_STARTED=false
ACTIVATION_COMMITTED=false
declare -a COMPOSE=()

log() {
    printf '[activate-admin] %s\n' "$*"
}

die() {
    printf '[activate-admin] ERROR: %s\n' "$*" >&2
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

effective_env_value() {
    local key="$1"

    if env_value "$key" "$RELEASE_ENV"; then
        return 0
    fi
    env_value "$key" "$APP_ENV"
}

compose() {
    "${COMPOSE[@]}" "$@"
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

wait_healthy() {
    local service="$1"
    local container_id
    local state

    for _attempt in $(seq 1 60); do
        container_id="$(compose_service_id "$service")" \
            || die "failed to enumerate ${service} while waiting for health"
        if [[ -n "$container_id" ]]; then
            state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")"
            if [[ "$state" == "healthy" || "$state" == "running" ]]; then
                printf '%s' "$container_id"
                return 0
            fi
            if [[ "$state" == "exited" || "$state" == "dead" ]]; then
                die "${service} stopped during activation"
            fi
        fi
        sleep 2
    done
    die "${service} did not become healthy"
}

assert_container_image() {
    local service="$1"
    local expected_image="$2"
    local container_id
    local actual_image
    local revision

    container_id="$(compose_service_id "$service")" \
        || die "failed to enumerate ${service}"
    [[ -n "$container_id" ]] || die "${service} is not running"
    actual_image="$(docker inspect -f '{{.Config.Image}}' "$container_id")"
    [[ "$actual_image" == "$expected_image" ]] \
        || die "${service} is not running the staged digest"
    revision="$(docker inspect -f '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$container_id")"
    [[ "$revision" == "$RELEASE_SHA" ]] \
        || die "${service} image revision does not match the staged release"
}

http_status() {
    local url="$1"

    curl --noproxy '*' --silent --show-error \
        --connect-timeout 3 --max-time 10 \
        --output /dev/null --write-out '%{http_code}' "$url"
}

assert_http_status() {
    local path="$1"
    local expected="$2"

    assert_url_status \
        "http://127.0.0.1:14081${path}" "$expected" "loopback ${path}"
}

assert_url_status() {
    local url="$1"
    local expected="$2"
    local label="$3"
    local actual

    actual="$(http_status "$url")" \
        || die "HTTP smoke failed for ${label}"
    [[ "$actual" == "$expected" ]] \
        || die "unexpected HTTP ${actual} for ${label}; expected ${expected}"
}

assert_exact_marker() {
    local marker="$1"
    local expected="$2"
    local label="$3"
    local actual

    [[ -s "$marker" ]] || die "${label} marker is missing"
    actual="$(<"$marker")"
    [[ "$actual" == "$expected" ]] || die "${label} marker does not match"
}

smoke_spa_at() {
    local url="$1"
    local label="$2"
    local body

    body="$(curl --noproxy '*' --fail --silent --show-error \
        --connect-timeout 3 --max-time 10 \
        "$url")" \
        || die "${label} smoke failed"
    grep -Eiq "<div[^>]+id=['\"]root['\"]" <<< "$body" \
        || die "${label} response is missing the application root"
}

smoke_loopback_routes() {
    smoke_spa_at "http://127.0.0.1:14081/" "admin SPA loopback"
    assert_http_status /api/admin/v1/auth/me 401
    for legacy_path in /api/users /api/configs /api/servers; do
        assert_http_status "$legacy_path" 404
    done
}

smoke_public_https() {
    smoke_spa_at "${ADMIN_PUBLIC_ORIGIN}/" "public HTTPS admin SPA"
    assert_url_status \
        "${ADMIN_PUBLIC_ORIGIN}/api/admin/v1/auth/me" \
        401 \
        "public HTTPS admin session endpoint"
}

smoke_backend_legacy_login_disabled() {
    local admin_container_id="$1"

    docker exec -i "$admin_container_id" python - <<'PY'
import urllib.error
import urllib.request

try:
    urllib.request.urlopen("http://127.0.0.1:8000/login", timeout=5)
except urllib.error.HTTPError as exc:
    if exc.code == 404:
        raise SystemExit(0)
    raise
raise SystemExit("legacy backend /login unexpectedly exists")
PY
}

wait_prometheus_targets() {
    local admin_container_id="$1"

    docker exec -i "$admin_container_id" python - <<'PY'
import json
import time
import urllib.request

wanted = {
    "vpn-hub-admin",
    "vpn-hub-statsd",
    "vpn-hub-postgres",
    "vpn-hub-redis",
}
deadline = time.monotonic() + 90
last = {}
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(
            "http://prometheus:9090/api/v1/targets", timeout=5
        ) as response:
            payload = json.load(response)
        last = {
            target.get("labels", {}).get("job", target.get("scrapePool")): target.get(
                "health"
            )
            for target in payload.get("data", {}).get("activeTargets", [])
        }
        if all(last.get(job) == "up" for job in wanted):
            raise SystemExit(0)
    except (OSError, ValueError):
        pass
    time.sleep(3)
raise SystemExit(f"Prometheus targets are not ready: {last}")
PY
}

run_smokes() {
    local admin_container_id

    admin_container_id="$(wait_healthy admin)"
    wait_healthy admin_frontend >/dev/null
    wait_healthy nginx >/dev/null
    for service in "${MONITORING_SERVICES[@]}"; do
        wait_healthy "$service" >/dev/null
    done

    assert_container_image admin "$(env_value VPN_ADMIN_IMAGE "$RELEASE_ENV")"
    assert_container_image \
        admin_frontend "$(env_value VPN_ADMIN_FRONTEND_IMAGE "$RELEASE_ENV")"
    assert_container_image nginx "$(env_value VPN_NGINX_IMAGE "$RELEASE_ENV")"

    smoke_loopback_routes
    smoke_backend_legacy_login_disabled "$admin_container_id" \
        || die "legacy backend /login route is enabled"
    wait_prometheus_targets "$admin_container_id" \
        || die "monitoring target smoke failed"
    smoke_public_https
}

stop_new_services() {
    local -a services=("${HUB_SERVICES[@]}" "${MONITORING_SERVICES[@]}")

    log "activation failed; stopping only the newly activated hub and monitoring services"
    if ! compose --profile hub --profile monitoring \
        stop --timeout 30 "${services[@]}"; then
        local service
        local output
        local -a ids=()

        log "Compose stop failed; falling back to exact service-labelled containers"
        for service in "${services[@]}"; do
            ids=()
            output="$(compose_service_ids "$service" || true)"
            if [[ -n "$output" ]]; then
                mapfile -t ids <<< "$output"
                docker stop --time 30 "${ids[@]}" >/dev/null 2>&1 || true
            fi
        done
    fi
}

on_exit() {
    local exit_code=$?

    trap - EXIT
    if ((exit_code != 0)) \
        && [[ "${ACTIVATION_STARTED:-false}" == "true" ]] \
        && [[ "${ACTIVATION_COMMITTED:-false}" != "true" ]]; then
        if [[ -n "${DEPLOY_ROOT:-}" ]] \
            && [[ -f "$DEPLOY_ROOT/current-admin-hub" ]] \
            && [[ "$(<"$DEPLOY_ROOT/current-admin-hub")" == "${RELEASE_SHA:-}" ]]; then
            rm -f "$DEPLOY_ROOT/current-admin-hub"
        fi
        stop_new_services
        log "database schema and the running bot were not changed or rolled back"
    fi
    exit "$exit_code"
}

write_activation_marker() {
    local marker="$DEPLOY_ROOT/current-admin-hub"
    local temporary

    temporary="$(mktemp "$DEPLOY_ROOT/.current-admin-hub.XXXXXX")"
    printf '%s\n' "$RELEASE_SHA" > "$temporary"
    chmod 0600 "$temporary"
    mv -f "$temporary" "$marker"
}

start_new_services() {
    log "pulling only the digest-pinned monitoring images"
    compose --profile monitoring pull "${MONITORING_SERVICES[@]}"
    log "starting the explicit monitoring service set"
    compose --profile monitoring up -d --no-deps "${MONITORING_SERVICES[@]}"

    log "starting the staged admin backend without dependencies or migrations"
    compose --profile hub up -d --no-deps --pull never admin
    wait_healthy admin >/dev/null
    log "starting the staged SPA and loopback-only proxy"
    compose --profile hub up -d --no-deps --pull never admin_frontend nginx
}

main() {
    local activation_marker
    local image_ref
    local key
    local rendered_images
    local running_id
    local revision
    local db_container_id
    local bot_container_id
    local actual_bot_image
    local database_user
    local exporter_user
    local external_volume
    local -a image_keys=(
        VPN_ADMIN_IMAGE
        VPN_BOT_IMAGE
        VPN_BILLING_IMAGE
        VPN_MIGRATIONS_IMAGE
        VPN_ADMIN_FRONTEND_IMAGE
        VPN_NGINX_IMAGE
    )
    local -A expected_policy=(
        [MAINTENANCE_MODE]=true
        [BILLING_ENABLED]=false
        [PAYMENTS_ENABLED]=false
        [PROVISIONING_ENABLED]=false
        [NOTIFICATIONS_ENABLED]=false
        [VPN_DRIFT_REPAIR_ENABLED]=false
    )

    [[ "$EUID" == "0" ]] || die "activation must run as root"
    : "${DEPLOY_ROOT:?DEPLOY_ROOT is required}"
    : "${RELEASE_DIR:?RELEASE_DIR is required}"
    : "${RELEASE_SHA:?RELEASE_SHA is required}"
    [[ "$DEPLOY_ROOT" == /* ]] || die "DEPLOY_ROOT must be absolute"
    [[ "$RELEASE_DIR" == "$DEPLOY_ROOT"/releases/* ]] \
        || die "unexpected RELEASE_DIR"
    [[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] \
        || die "RELEASE_SHA must be a full commit SHA"

    for command_name in curl docker flock grep mktemp seq; do
        require_command "$command_name"
    done
    docker compose version >/dev/null

    exec 9>/var/lock/vpn-hub-deploy.lock
    flock -n 9 || die "another production deployment is already running"

    APP_ENV="$DEPLOY_ROOT/.env"
    RELEASE_ENV="$RELEASE_DIR/release.env"
    COMPOSE_FILE="$RELEASE_DIR/docker-compose-prod.yml"
    activation_marker="$DEPLOY_ROOT/current-admin-hub"

    for required_file in \
        "$APP_ENV" \
        "$RELEASE_ENV" \
        "$COMPOSE_FILE" \
        "$RELEASE_DIR/observability/alerts.yml" \
        "$RELEASE_DIR/observability/prometheus.yml" \
        "$RELEASE_DIR/observability/statsd-mapping.yml"; do
        [[ -s "$required_file" ]] || die "missing staged release file: $required_file"
    done
    chmod 0600 "$APP_ENV" "$RELEASE_ENV"

    assert_exact_marker \
        "$DEPLOY_ROOT/current-release" "$RELEASE_SHA" "successful bot canary"
    [[ "$(env_value RELEASE_SHA "$RELEASE_ENV")" == "$RELEASE_SHA" ]] \
        || die "staged release manifest SHA mismatch"
    ADMIN_PUBLIC_ORIGIN="$(env_value ADMIN_PUBLIC_ORIGIN "$RELEASE_ENV")" \
        || die "staged release is missing ADMIN_PUBLIC_ORIGIN"
    [[ "$ADMIN_PUBLIC_ORIGIN" =~ ^https://[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]{1,5})?$ ]] \
        || die "ADMIN_PUBLIC_ORIGIN must be a path-free HTTPS origin"

    for key in "${!expected_policy[@]}"; do
        [[ "$(env_value "$key" "$RELEASE_ENV")" == "${expected_policy[$key]}" ]] \
            || die "unsafe staged release policy: ${key}"
    done
    [[ "$(env_value VPN_ENV_FILE "$RELEASE_ENV")" == "$APP_ENV" ]] \
        || die "release points at an unexpected application environment"
    for key in POSTGRES_EXPORTER_USER POSTGRES_EXPORTER_PASSWORD; do
        effective_env_value "$key" >/dev/null \
            || die "production environment is missing ${key}"
    done
    exporter_user="$(effective_env_value POSTGRES_EXPORTER_USER)"
    database_user="$(effective_env_value POSTGRES_USER)" \
        || die "production environment is missing POSTGRES_USER"
    [[ "$exporter_user" == "$POSTGRES_EXPORTER_ROLE" ]] \
        || die "production exporter identity is not the dedicated role"
    [[ "$exporter_user" != "$database_user" ]] \
        || die "production exporter identity aliases POSTGRES_USER"
    for external_volume in vpn_redis_data vpn_prometheus_data; do
        docker volume inspect "$external_volume" >/dev/null \
            || die "required external volume ${external_volume} is missing"
    done

    for key in "${image_keys[@]}"; do
        image_ref="$(env_value "$key" "$RELEASE_ENV")"
        [[ "$image_ref" =~ ^docker\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$ ]] \
            || die "${key} is not pinned by registry digest"
        docker image inspect "$image_ref" >/dev/null \
            || die "staged image is not loaded locally: ${key}"
        revision="$(docker image inspect -f '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image_ref")"
        [[ "$revision" == "$RELEASE_SHA" ]] \
            || die "staged image revision mismatch for ${key}"
    done

    COMPOSE=(
        env OBSERVABILITY_ENABLED=true
        docker compose
        --project-directory "$RELEASE_DIR"
        --env-file "$APP_ENV"
        --env-file "$RELEASE_ENV"
        -f "$COMPOSE_FILE"
    )
    compose --profile hub --profile monitoring config --quiet
    rendered_images="$(compose --profile hub --profile monitoring config --images)" \
        || die "failed to enumerate staged images"
    if grep -Ev '@sha256:[0-9a-f]{64}$' <<< "$rendered_images"; then
        die "staged Compose contains an image that is not pinned by digest"
    fi

    db_container_id="$(compose_service_id db)" \
        || die "failed to enumerate the production database"
    [[ -n "$db_container_id" ]] || die "the production database is not running"
    revision="$(docker exec -i "$db_container_id" sh -ec \
        'exec psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT version_num FROM alembic_version"')" \
        || die "could not verify the live schema revision"
    [[ "$revision" == "$EXPECTED_REVISION" ]] \
        || die "live schema is not at the canary-tested revision"
    wait_healthy redis >/dev/null

    bot_container_id="$(compose_service_id bot)" \
        || die "failed to enumerate the bot canary"
    [[ -n "$bot_container_id" ]] || die "the bot canary is not running"
    actual_bot_image="$(docker inspect -f '{{.Config.Image}}' "$bot_container_id")"
    [[ "$actual_bot_image" == "$(env_value VPN_BOT_IMAGE "$RELEASE_ENV")" ]] \
        || die "the running bot is not from the staged release"
    [[ "$(docker inspect -f '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$bot_container_id")" == "$RELEASE_SHA" ]] \
        || die "the running bot revision does not match the staged release"

    if [[ -e "$activation_marker" ]]; then
        assert_exact_marker "$activation_marker" "$RELEASE_SHA" "admin hub release"
        log "admin hub release is already active; re-running read-only smokes"
        run_smokes
        log "admin hub activation already healthy"
        return 0
    fi

    for service in "${HUB_SERVICES[@]}" "${MONITORING_SERVICES[@]}"; do
        running_id="$(compose_service_id "$service")" \
            || die "multiple running containers found for ${service}"
        [[ -z "$running_id" ]] \
            || die "unexpected pre-existing ${service} container"
    done

    ACTIVATION_STARTED=true
    start_new_services

    run_smokes
    write_activation_marker
    ACTIVATION_COMMITTED=true
    log "admin hub and monitoring activation complete for ${RELEASE_SHA}"
    log "the bot and all financial mutation switches were left unchanged"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    trap on_exit EXIT
    main "$@"
fi
