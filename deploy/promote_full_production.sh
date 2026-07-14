#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

readonly EXPECTED_REVISION="d4e7f9a1b2c3"
readonly -a MONITORING_SERVICES=(
    statsd_exporter
    postgres_exporter
    redis_exporter
    prometheus
)
readonly -a MUTATION_SERVICES=(bot rq_worker rq_scheduler)

APP_ENV=""
STAGED_RELEASE_ENV=""
LIVE_RELEASE_ENV=""
COMPOSE_FILE=""
ADMIN_PUBLIC_ORIGIN=""
BACKUP_ENV=""
IN_PROGRESS_MARKER=""
PROMOTION_STARTED=false
PROMOTION_COMMITTED=false
declare -a COMPOSE=()

log() {
    printf '[promote] %s\n' "$*"
}

die() {
    printf '[promote] ERROR: %s\n' "$*" >&2
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
                die "${service} stopped during production promotion"
            fi
        fi
        sleep 2
    done
    die "${service} did not become healthy"
}

wait_stably_running() {
    local service="$1"
    local checks="${2:-8}"
    local container_id=""

    for _attempt in $(seq 1 "$checks"); do
        container_id="$(compose_service_id "$service")" \
            || die "failed to enumerate ${service} while observing it"
        [[ -n "$container_id" ]] || die "${service} is not running"
        [[ "$(docker inspect -f '{{.State.Running}}' "$container_id")" == "true" ]] \
            || die "${service} stopped during production promotion"
        sleep 2
    done
    printf '%s' "$container_id"
}

assert_exact_marker() {
    local marker="$1"
    local expected="$2"
    local label="$3"
    local actual

    [[ -f "$marker" && ! -L "$marker" ]] || die "${label} marker is missing or unsafe"
    actual="$(<"$marker")"
    [[ "$actual" == "$expected" ]] || die "${label} marker does not match"
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

assert_runtime_policy() {
    local service="$1"
    local container_id
    local runtime_environment
    local key
    local -A expected_policy=(
        [MAINTENANCE_MODE]=false
        [BILLING_ENABLED]=true
        [PAYMENTS_ENABLED]=true
        [PROVISIONING_ENABLED]=true
        [NOTIFICATIONS_ENABLED]=false
        [REFERRAL_REWARDS_ENABLED]=true
        [VPN_DRIFT_REPAIR_ENABLED]=false
        [OBSERVABILITY_ENABLED]=true
    )

    container_id="$(compose_service_id "$service")" \
        || die "failed to enumerate ${service} runtime environment"
    [[ -n "$container_id" ]] || die "${service} is not running"
    runtime_environment="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$container_id")"
    for key in "${!expected_policy[@]}"; do
        if [[ "$service" == "rq_scheduler" && "$key" == "VPN_DRIFT_REPAIR_ENABLED" ]]; then
            continue
        fi
        grep -Fx "${key}=${expected_policy[$key]}" <<< "$runtime_environment" >/dev/null \
            || die "${service} did not load promoted ${key} policy"
    done
}

assert_policy() {
    local file="$1"
    local phase="$2"
    local key
    local -A expected_policy=()

    if [[ "$phase" == "staged" ]]; then
        expected_policy=(
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
    elif [[ "$phase" == "promoted" ]]; then
        expected_policy=(
            [MAINTENANCE_MODE]=false
            [BILLING_ENABLED]=true
            [PAYMENTS_ENABLED]=true
            [PROVISIONING_ENABLED]=true
            [NOTIFICATIONS_ENABLED]=false
            [REFERRAL_REWARDS_ENABLED]=true
            [REFERRAL_LEVEL_1_RATE_BPS]=500
            [REFERRAL_LEVEL_2_RATE_BPS]=100
            [REFERRAL_PROGRAM_VERSION]=v1-5pct-1pct
            [VPN_DRIFT_REPAIR_ENABLED]=false
            [OBSERVABILITY_ENABLED]=true
        )
    else
        die "unknown release policy phase: ${phase}"
    fi

    for key in "${!expected_policy[@]}"; do
        [[ "$(env_value "$key" "$file")" == "${expected_policy[$key]}" ]] \
            || die "unexpected ${phase} release policy: ${key}"
    done
}

build_promoted_release_env() {
    local source="$1"
    local destination="$2"

    install -m 0600 "$source" "$destination"
    set_env_value MAINTENANCE_MODE false "$destination"
    set_env_value BILLING_ENABLED true "$destination"
    set_env_value PAYMENTS_ENABLED true "$destination"
    set_env_value PROVISIONING_ENABLED true "$destination"
    set_env_value NOTIFICATIONS_ENABLED false "$destination"
    set_env_value REFERRAL_REWARDS_ENABLED true "$destination"
    set_env_value VPN_DRIFT_REPAIR_ENABLED false "$destination"
    set_env_value OBSERVABILITY_ENABLED true "$destination"
    assert_policy "$destination" promoted
}

http_status() {
    local url="$1"

    curl --noproxy '*' --silent --show-error \
        --connect-timeout 3 --max-time 10 \
        --output /dev/null --write-out '%{http_code}' "$url"
}

assert_url_status() {
    local url="$1"
    local expected="$2"
    local label="$3"
    local actual

    actual="$(http_status "$url")" || die "HTTP smoke failed for ${label}"
    [[ "$actual" == "$expected" ]] \
        || die "unexpected HTTP ${actual} for ${label}; expected ${expected}"
}

smoke_spa_at() {
    local url="$1"
    local label="$2"
    local body

    body="$(curl --noproxy '*' --fail --silent --show-error \
        --connect-timeout 3 --max-time 10 "$url")" \
        || die "${label} smoke failed"
    grep -Eiq "<div[^>]+id=['\"]root['\"]" <<< "$body" \
        || die "${label} response is missing the application root"
}

run_admin_smokes() {
    smoke_spa_at "http://127.0.0.1:14081/" "admin SPA loopback"
    assert_url_status \
        "http://127.0.0.1:14081/api/admin/v1/auth/me" 401 \
        "loopback admin session endpoint"
    for legacy_path in /api/users /api/configs /api/servers; do
        assert_url_status \
            "http://127.0.0.1:14081${legacy_path}" 404 \
            "disabled legacy route ${legacy_path}"
    done
    smoke_spa_at "${ADMIN_PUBLIC_ORIGIN}/" "public HTTPS admin SPA"
    assert_url_status \
        "${ADMIN_PUBLIC_ORIGIN}/api/admin/v1/auth/me" 401 \
        "public HTTPS admin session endpoint"
}

run_read_only_preflights() {
    local db_container_id

    db_container_id="$(compose_service_id db)" \
        || die "failed to enumerate the production database"
    [[ -n "$db_container_id" ]] || die "the production database is not running"
    docker exec -i "$db_container_id" sh -ec \
        'exec psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
        < "$RELEASE_DIR/preflight.sql"

    log "verifying Manager mTLS identity, readiness and OpenVPN data plane"
    compose --profile bot exec -T bot python - < "$RELEASE_DIR/manager_smoke.py"
    log "verifying the Telegram bot identity without sending a message"
    compose --profile bot exec -T bot python - < "$RELEASE_DIR/telegram_smoke.py"
}

scan_service_logs() {
    local service="$1"
    local since="$2"
    local container_id
    local output

    container_id="$(compose_service_id "$service")" \
        || die "failed to enumerate ${service} logs"
    [[ -n "$container_id" ]] || die "${service} is not running"
    output="$(docker logs --since "$since" "$container_id" 2>&1)" \
        || die "failed to read ${service} logs"
    if grep -Eqi 'Unauthorized|Conflict: terminated by other getUpdates|Traceback|CRITICAL' <<< "$output"; then
        die "${service} emitted a fatal startup error"
    fi
}

start_full_services() {
    local started_at

    started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    log "recreating the admin backend with the promoted policy"
    compose --profile hub up -d --no-deps --pull never --force-recreate admin
    wait_healthy admin >/dev/null
    assert_runtime_policy admin
    wait_healthy admin_frontend >/dev/null
    wait_healthy nginx >/dev/null
    run_admin_smokes

    log "starting the RQ worker before reopening bot ingress"
    compose --profile worker up -d --no-deps --pull never --force-recreate rq_worker
    wait_stably_running rq_worker 5 >/dev/null
    assert_runtime_policy rq_worker
    assert_container_image rq_worker "$(env_value VPN_BILLING_IMAGE "$LIVE_RELEASE_ENV")"
    scan_service_logs rq_worker "$started_at"

    log "recreating the Telegram bot with payments and provisioning enabled"
    compose --profile bot up -d --no-deps --pull never --force-recreate bot
    wait_stably_running bot 8 >/dev/null
    assert_runtime_policy bot
    scan_service_logs bot "$started_at"
    run_read_only_preflights

    log "starting the billing scheduler last"
    compose --profile billing-scheduler up -d --no-deps --pull never --force-recreate rq_scheduler
    wait_stably_running rq_scheduler 5 >/dev/null
    assert_runtime_policy rq_scheduler
    assert_container_image rq_scheduler "$(env_value VPN_BILLING_IMAGE "$LIVE_RELEASE_ENV")"
    scan_service_logs rq_scheduler "$started_at"
}

stop_promoted_services() {
    local service
    local output
    local all_stopped=true
    local -a ids=()

    log "stopping scheduler, worker, bot and admin before restoring the fail-closed policy"
    if ! compose \
        --profile hub \
        --profile bot \
        --profile worker \
        --profile billing-scheduler \
        stop --timeout 60 rq_scheduler rq_worker bot admin >/dev/null 2>&1; then
        log "Compose stop failed; stopping exact service-labelled containers"
    fi

    for service in rq_scheduler rq_worker bot admin; do
        ids=()
        output="$(compose_service_ids "$service" || true)"
        if [[ -n "$output" ]]; then
            mapfile -t ids <<< "$output"
            docker stop --time 60 "${ids[@]}" >/dev/null 2>&1 || true
        fi
        ids=()
        output="$(compose_service_ids "$service" || true)"
        if [[ -n "$output" ]]; then
            mapfile -t ids <<< "$output"
            docker kill "${ids[@]}" >/dev/null 2>&1 || true
        fi
        if [[ -n "$(compose_service_ids "$service" || true)" ]]; then
            log "CRITICAL: ${service} could not be stopped; manual recovery is required"
            all_stopped=false
        fi
    done
    [[ "$all_stopped" == "true" ]]
}

restore_fail_closed_runtime() {
    local temporary
    local workers_stopped=false
    local environment_restored=false

    if stop_promoted_services; then
        workers_stopped=true
    fi
    if [[ -s "$BACKUP_ENV" ]]; then
        temporary="$(mktemp "$DEPLOY_ROOT/.release.env.rollback.XXXXXX")"
        install -m 0600 "$BACKUP_ENV" "$temporary"
        mv -f "$temporary" "$LIVE_RELEASE_ENV"
        environment_restored=true
        log "restored the pre-promotion release environment"

        compose --profile hub up -d --no-deps --pull never --force-recreate admin \
            >/dev/null 2>&1 || log "manual recovery required for admin"
        compose --profile bot up -d --no-deps --pull never --force-recreate bot \
            >/dev/null 2>&1 || log "manual recovery required for bot"
    else
        log "release environment backup is unavailable; manual recovery is required"
    fi
    if [[ -f "$DEPLOY_ROOT/current-production" && ! -L "$DEPLOY_ROOT/current-production" ]]; then
        if [[ "$(<"$DEPLOY_ROOT/current-production")" == "$RELEASE_SHA" ]]; then
            rm -f "$DEPLOY_ROOT/current-production"
        fi
    fi
    if [[ "$workers_stopped" == "true" && "$environment_restored" == "true" ]]; then
        rm -f "$IN_PROGRESS_MARKER"
    else
        log "promotion-in-progress was preserved for required manual recovery"
    fi
}

on_exit() {
    local exit_code=$?

    trap - EXIT
    if ((exit_code != 0)) \
        && [[ "${PROMOTION_STARTED:-false}" == "true" ]] \
        && [[ "${PROMOTION_COMMITTED:-false}" != "true" ]]; then
        log "promotion failed; returning runtime services to the staged fail-closed policy"
        restore_fail_closed_runtime
        log "committed database, Telegram, payment, or VPN side effects were not reversed"
    fi
    exit "$exit_code"
}

write_marker() {
    local marker="$DEPLOY_ROOT/current-production"
    local temporary

    temporary="$(mktemp "$DEPLOY_ROOT/.current-production.XXXXXX")"
    printf '%s\n' "$RELEASE_SHA" > "$temporary"
    chmod 0600 "$temporary"
    mv -f "$temporary" "$marker"
}

assert_base_topology() {
    local service
    local container_id

    wait_healthy db >/dev/null
    wait_healthy redis >/dev/null
    wait_healthy admin >/dev/null
    wait_healthy admin_frontend >/dev/null
    wait_healthy nginx >/dev/null
    wait_stably_running bot 1 >/dev/null
    for service in "${MONITORING_SERVICES[@]}"; do
        wait_healthy "$service" >/dev/null
    done

    assert_container_image admin "$(env_value VPN_ADMIN_IMAGE "$STAGED_RELEASE_ENV")"
    assert_container_image bot "$(env_value VPN_BOT_IMAGE "$STAGED_RELEASE_ENV")"
    assert_container_image \
        admin_frontend "$(env_value VPN_ADMIN_FRONTEND_IMAGE "$STAGED_RELEASE_ENV")"
    assert_container_image nginx "$(env_value VPN_NGINX_IMAGE "$STAGED_RELEASE_ENV")"

    for service in rq_worker rq_scheduler; do
        container_id="$(compose_service_id "$service")" \
            || die "multiple running containers found for ${service}"
        [[ -z "$container_id" ]] \
            || die "unexpected pre-existing ${service} container before promotion"
    done
}

assert_full_topology() {
    local service

    for service in db redis admin admin_frontend nginx "${MONITORING_SERVICES[@]}"; do
        wait_healthy "$service" >/dev/null
    done
    for service in "${MUTATION_SERVICES[@]}"; do
        wait_stably_running "$service" 1 >/dev/null
        assert_runtime_policy "$service"
    done
    assert_runtime_policy admin
    assert_container_image admin "$(env_value VPN_ADMIN_IMAGE "$LIVE_RELEASE_ENV")"
    assert_container_image bot "$(env_value VPN_BOT_IMAGE "$LIVE_RELEASE_ENV")"
    assert_container_image rq_worker "$(env_value VPN_BILLING_IMAGE "$LIVE_RELEASE_ENV")"
    assert_container_image rq_scheduler "$(env_value VPN_BILLING_IMAGE "$LIVE_RELEASE_ENV")"
    assert_container_image \
        admin_frontend "$(env_value VPN_ADMIN_FRONTEND_IMAGE "$LIVE_RELEASE_ENV")"
    assert_container_image nginx "$(env_value VPN_NGINX_IMAGE "$LIVE_RELEASE_ENV")"
}

main() {
    local image_ref
    local key
    local rendered_images
    local revision
    local db_container_id
    local promotion_marker
    local candidate
    local timestamp
    local backup_dir
    local -a image_keys=(
        VPN_ADMIN_IMAGE
        VPN_BOT_IMAGE
        VPN_BILLING_IMAGE
        VPN_MIGRATIONS_IMAGE
        VPN_ADMIN_FRONTEND_IMAGE
        VPN_NGINX_IMAGE
    )

    [[ "$EUID" == "0" ]] || die "production promotion must run as root"
    : "${DEPLOY_ROOT:?DEPLOY_ROOT is required}"
    : "${RELEASE_DIR:?RELEASE_DIR is required}"
    : "${RELEASE_SHA:?RELEASE_SHA is required}"
    [[ "$DEPLOY_ROOT" == /* ]] || die "DEPLOY_ROOT must be absolute"
    [[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] \
        || die "RELEASE_SHA must be a full commit SHA"
    [[ "$RELEASE_DIR" == "$DEPLOY_ROOT/releases/$RELEASE_SHA" ]] \
        || die "RELEASE_DIR must be the exact staged release directory"
    promotion_marker="$DEPLOY_ROOT/current-production"

    for command_name in awk curl date docker flock grep install mktemp mv seq sha256sum; do
        require_command "$command_name"
    done
    docker compose version >/dev/null

    exec 9>/var/lock/vpn-hub-deploy.lock
    flock -n 9 || die "another production deployment is already running"

    APP_ENV="$DEPLOY_ROOT/.env"
    STAGED_RELEASE_ENV="$RELEASE_DIR/release.env"
    LIVE_RELEASE_ENV="$DEPLOY_ROOT/release.env"
    COMPOSE_FILE="$RELEASE_DIR/docker-compose-prod.yml"
    IN_PROGRESS_MARKER="$DEPLOY_ROOT/promotion-in-progress"

    for required_file in \
        "$APP_ENV" \
        "$STAGED_RELEASE_ENV" \
        "$LIVE_RELEASE_ENV" \
        "$COMPOSE_FILE" \
        "$RELEASE_DIR/preflight.sql" \
        "$RELEASE_DIR/manager_smoke.py" \
        "$RELEASE_DIR/telegram_smoke.py"; do
        [[ -f "$required_file" && ! -L "$required_file" && -s "$required_file" ]] \
            || die "missing or unsafe production release file: $required_file"
    done
    chmod 0600 "$APP_ENV" "$STAGED_RELEASE_ENV" "$LIVE_RELEASE_ENV"
    [[ ! -e "$IN_PROGRESS_MARKER" ]] \
        || die "an interrupted promotion marker exists; inspect and recover it first"

    assert_exact_marker \
        "$DEPLOY_ROOT/current-release" "$RELEASE_SHA" "successful bot canary"
    assert_exact_marker \
        "$DEPLOY_ROOT/current-admin-hub" "$RELEASE_SHA" "successful admin activation"
    [[ "$(env_value RELEASE_SHA "$STAGED_RELEASE_ENV")" == "$RELEASE_SHA" ]] \
        || die "staged release manifest SHA mismatch"
    [[ "$(env_value RELEASE_SHA "$LIVE_RELEASE_ENV")" == "$RELEASE_SHA" ]] \
        || die "live release manifest SHA mismatch"
    assert_policy "$STAGED_RELEASE_ENV" staged

    ADMIN_PUBLIC_ORIGIN="$(env_value ADMIN_PUBLIC_ORIGIN "$STAGED_RELEASE_ENV")" \
        || die "staged release is missing ADMIN_PUBLIC_ORIGIN"
    [[ "$ADMIN_PUBLIC_ORIGIN" =~ ^https://[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]{1,5})?$ ]] \
        || die "ADMIN_PUBLIC_ORIGIN must be a path-free HTTPS origin"
    [[ "$(env_value VPN_ENV_FILE "$STAGED_RELEASE_ENV")" == "$APP_ENV" ]] \
        || die "release points at an unexpected application environment"
    [[ "$(env_value VPN_MANAGER_TLS_DIR_PROD "$STAGED_RELEASE_ENV")" == "/etc/vpn-hub/manager-pki" ]] \
        || die "release points at an unexpected Manager TLS directory"

    for key in "${image_keys[@]}"; do
        image_ref="$(env_value "$key" "$STAGED_RELEASE_ENV")"
        [[ "$image_ref" =~ ^docker\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$ ]] \
            || die "${key} is not pinned by registry digest"
        docker image inspect "$image_ref" >/dev/null \
            || die "staged image is not loaded locally: ${key}"
        revision="$(docker image inspect -f '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$image_ref")"
        [[ "$revision" == "$RELEASE_SHA" ]] \
            || die "staged image revision mismatch for ${key}"
    done

    COMPOSE=(
        docker compose
        --project-directory "$RELEASE_DIR"
        --env-file "$APP_ENV"
        --env-file "$LIVE_RELEASE_ENV"
        -f "$COMPOSE_FILE"
    )
    compose \
        --profile hub \
        --profile bot \
        --profile worker \
        --profile billing-scheduler \
        --profile monitoring \
        config --quiet
    rendered_images="$(compose \
        --profile hub \
        --profile bot \
        --profile worker \
        --profile billing-scheduler \
        --profile monitoring \
        config --images)" || die "failed to enumerate production images"
    if grep -Ev '@sha256:[0-9a-f]{64}$' <<< "$rendered_images"; then
        die "production Compose contains an image that is not pinned by digest"
    fi

    db_container_id="$(compose_service_id db)" \
        || die "failed to enumerate the production database"
    [[ -n "$db_container_id" ]] || die "the production database is not running"
    revision="$(docker exec -i "$db_container_id" sh -ec \
        'exec psql -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT version_num FROM alembic_version"')" \
        || die "could not verify the live schema revision"
    [[ "$revision" == "$EXPECTED_REVISION" ]] \
        || die "live schema is not at the canary-tested revision"

    if [[ -e "$promotion_marker" ]]; then
        assert_exact_marker "$promotion_marker" "$RELEASE_SHA" "full production"
        assert_policy "$LIVE_RELEASE_ENV" promoted
        assert_full_topology
        run_admin_smokes
        run_read_only_preflights
        log "full production release is already active and healthy"
        return 0
    fi

    assert_policy "$LIVE_RELEASE_ENV" staged
    assert_base_topology
    run_admin_smokes
    run_read_only_preflights

    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    backup_dir="$DEPLOY_ROOT/backups/${timestamp}-${RELEASE_SHA:0:12}-promotion"
    install -d -m 0700 "$backup_dir"
    BACKUP_ENV="$backup_dir/release.env.before"
    install -m 0600 "$LIVE_RELEASE_ENV" "$BACKUP_ENV"
    sha256sum "$BACKUP_ENV" > "$BACKUP_ENV.sha256"
    chmod 0600 "$BACKUP_ENV.sha256"
    sha256sum --check "$BACKUP_ENV.sha256" >/dev/null

    candidate="$(mktemp "$DEPLOY_ROOT/.release.env.promoted.XXXXXX")"
    build_promoted_release_env "$STAGED_RELEASE_ENV" "$candidate"

    {
        printf 'RELEASE_SHA=%s\n' "$RELEASE_SHA"
        printf 'BACKUP_ENV=%s\n' "$BACKUP_ENV"
    } > "$IN_PROGRESS_MARKER"
    chmod 0600 "$IN_PROGRESS_MARKER"
    PROMOTION_STARTED=true
    mv -f "$candidate" "$LIVE_RELEASE_ENV"
    sha256sum "$LIVE_RELEASE_ENV" > "$backup_dir/release.env.promoted.sha256"
    chmod 0600 "$backup_dir/release.env.promoted.sha256"

    start_full_services
    assert_full_topology
    write_marker
    rm -f "$IN_PROGRESS_MARKER"
    PROMOTION_COMMITTED=true

    log "full production promotion complete for ${RELEASE_SHA}"
    log "billing, payments and provisioning are enabled"
    log "automatic Telegram notifications remain disabled"
    log "automatic VPN drift repair remains disabled"
    log "release environment backup: ${BACKUP_ENV}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    trap on_exit EXIT
    main "$@"
fi
