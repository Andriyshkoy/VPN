#!/usr/bin/env bash
# shellcheck disable=SC1090,SC1091,SC2034,SC2317
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EVENTS_FILE="$(mktemp)"
RUNTIME_ENV_FILE="$(mktemp)"
EXPORTER_SQL_FILE="$(mktemp)"
readonly REPO_ROOT EVENTS_FILE RUNTIME_ENV_FILE EXPORTER_SQL_FILE
trap 'rm -f "$EVENTS_FILE" "$RUNTIME_ENV_FILE" "$EXPORTER_SQL_FILE"' EXIT

# shellcheck source=../deploy/production_canary.sh
source "$REPO_ROOT/deploy/production_canary.sh"

[[ "$EXPECTED_REVISION" == "d4e7f9a1b2c3" ]]
[[ "$EXPECTED_PREVIOUS_REVISION" == "f1a8c3d9e742" ]]
[[ "$POSTGRES_EXPORTER_ROLE" == "vpn_exporter" ]]

interactive_smoke_count="$(
    grep -Fc \
        'if ! docker run --rm -i' \
        "$REPO_ROOT/deploy/production_canary.sh"
)"
[[ "$interactive_smoke_count" == "2" ]]

# First production use must generate non-empty exporter/Redis credentials and
# both external volumes before Compose is allowed to render or start services.
printf '%s\n' \
    'POSTGRES_DB=vpn' \
    'POSTGRES_USER=vpn' \
    > "$RUNTIME_ENV_FILE"
APP_ENV="$RUNTIME_ENV_FILE"
declare -A CREATED_VOLUMES=()
openssl() {
    [[ "$*" == "rand -hex 32" ]]
    printf '%s' '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
}
docker() {
    local volume

    [[ "$1" == "volume" ]]
    volume="$3"
    case "$2" in
        inspect)
            [[ "${CREATED_VOLUMES[$volume]:-}" == "true" ]]
            ;;
        create)
            CREATED_VOLUMES["$volume"]=true
            printf 'created:%s\n' "$volume" >> "$EVENTS_FILE"
            ;;
        *)
            return 2
            ;;
    esac
}
log() {
    :
}
prepare_runtime_credentials_and_volumes
[[ "$(env_value POSTGRES_EXPORTER_USER "$APP_ENV")" == "vpn_exporter" ]]
env_value POSTGRES_EXPORTER_PASSWORD "$APP_ENV" >/dev/null
env_value REDIS_PASSWORD "$APP_ENV" >/dev/null
[[ "$(<"$EVENTS_FILE")" == $'created:vpn_redis_data\ncreated:vpn_prometheus_data' ]]

# The exporter identity must never alias the application/database owner.
set_env_value POSTGRES_USER vpn_exporter "$APP_ENV"
if (prepare_runtime_credentials_and_volumes) 2>/dev/null; then
    printf 'exporter/database role collision was accepted\n' >&2
    exit 1
fi
set_env_value POSTGRES_USER vpn "$APP_ENV"

# Bootstrap SQL is fixed to the dedicated role, grants only pg_monitor, and
# validates both role attributes/membership and a password-authenticated login.
DB_CONTAINER_ID=db-container
db_psql() {
    case "$*" in
        *rolcanlogin*)
            printf 't'
            ;;
        *string_agg*)
            printf 'pg_monitor'
            ;;
        *)
            return 2
            ;;
    esac
}
docker() {
    if [[ "$1" == "exec" && "$2" == "-i" ]]; then
        sed -n '1,200p' > "$EXPORTER_SQL_FILE"
        return 0
    fi
    if [[ "$1" == "exec" && "$2" == "-e" ]]; then
        printf '1'
        return 0
    fi
    return 2
}
bootstrap_postgres_exporter_role
grep -F 'ALTER ROLE vpn_exporter WITH' "$EXPORTER_SQL_FILE" >/dev/null
grep -F 'GRANT pg_monitor TO vpn_exporter;' "$EXPORTER_SQL_FILE" >/dev/null
grep -F '\getenv exporter_password POSTGRES_EXPORTER_PASSWORD' \
    "$EXPORTER_SQL_FILE" >/dev/null
if grep -F '0123456789abcdef' "$EXPORTER_SQL_FILE" >/dev/null; then
    printf 'exporter credential leaked into bootstrap SQL\n' >&2
    exit 1
fi

: > "$EVENTS_FILE"

status=0
if (
    cleanup_preflight() {
        printf 'cleanup-preflight\n' >> "$EVENTS_FILE"
    }

    compose_service_ids() {
        if [[ "$1" == "bot" ]]; then
            printf 'bot-container-id\nbot-container-id-2\n'
        fi
    }

    docker() {
        printf 'docker:%s\n' "$*" >> "$EVENTS_FILE"
    }

    LIVE_MIGRATION_STARTED=true
    PREFLIGHT_CONTAINER=preflight-container
    PREFLIGHT_NETWORK=preflight-network
    trap on_exit EXIT
    die "forced post-migration failure"
); then
    printf 'expected the forced deployment failure to be non-zero\n' >&2
    exit 1
else
    status=$?
fi

[[ "$status" == "1" ]]
grep -Fx 'cleanup-preflight' "$EVENTS_FILE" >/dev/null
grep -Fx \
    'docker:stop --time 30 bot-container-id bot-container-id-2' \
    "$EVENTS_FILE" >/dev/null

printf 'production canary cleanup regression: ok\n'
