#!/usr/bin/env bash
# shellcheck disable=SC1090,SC1091,SC2016,SC2034,SC2329
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d)"
EVENTS_FILE="$TEST_ROOT/events"
readonly REPO_ROOT TEST_ROOT EVENTS_FILE
trap 'rm -rf "$TEST_ROOT"' EXIT

# shellcheck source=../deploy/promote_full_production.sh
source "$REPO_ROOT/deploy/promote_full_production.sh"

fail() {
    printf 'full production promotion regression: %s\n' "$*" >&2
    exit 1
}

write_staged_env() {
    local destination="$1"

    {
        printf 'RELEASE_SHA=0123456789abcdef0123456789abcdef01234567\n'
        printf 'VPN_ADMIN_IMAGE=docker.io/example/admin@sha256:%064d\n' 0
        printf 'VPN_BOT_IMAGE=docker.io/example/bot@sha256:%064d\n' 1
        printf 'VPN_BILLING_IMAGE=docker.io/example/billing@sha256:%064d\n' 2
        printf 'VPN_MIGRATIONS_IMAGE=docker.io/example/migrations@sha256:%064d\n' 3
        printf 'VPN_ADMIN_FRONTEND_IMAGE=docker.io/example/frontend@sha256:%064d\n' 4
        printf 'VPN_NGINX_IMAGE=docker.io/example/nginx@sha256:%064d\n' 5
        printf 'VPN_ENV_FILE=/opt/vpn/.env\n'
        printf 'VPN_MANAGER_TLS_DIR_PROD=/etc/vpn-hub/manager-pki\n'
        printf 'ADMIN_PUBLIC_ORIGIN=https://admin.example.test\n'
        printf 'MAINTENANCE_MODE=true\n'
        printf 'BILLING_ENABLED=false\n'
        printf 'PAYMENTS_ENABLED=false\n'
        printf 'PROVISIONING_ENABLED=false\n'
        printf 'NOTIFICATIONS_ENABLED=false\n'
        printf 'REFERRAL_REWARDS_ENABLED=true\n'
        printf 'REFERRAL_LEVEL_1_RATE_BPS=500\n'
        printf 'REFERRAL_LEVEL_2_RATE_BPS=100\n'
        printf 'REFERRAL_PROGRAM_VERSION=v1-5pct-1pct\n'
        printf 'VPN_DRIFT_REPAIR_ENABLED=false\n'
        printf 'OBSERVABILITY_ENABLED=false\n'
    } > "$destination"
    chmod 0600 "$destination"
}

STAGED="$TEST_ROOT/staged.env"
PROMOTED="$TEST_ROOT/promoted.env"
write_staged_env "$STAGED"
assert_policy "$STAGED" staged
build_promoted_release_env "$STAGED" "$PROMOTED"
assert_policy "$PROMOTED" promoted

for expected in \
    MAINTENANCE_MODE=false \
    BILLING_ENABLED=true \
    PAYMENTS_ENABLED=true \
    PROVISIONING_ENABLED=true \
    NOTIFICATIONS_ENABLED=false \
    REFERRAL_REWARDS_ENABLED=true \
    VPN_DRIFT_REPAIR_ENABLED=false \
    OBSERVABILITY_ENABLED=true; do
    [[ "$(grep -Fxc "$expected" "$PROMOTED")" == "1" ]] \
        || fail "promoted environment does not contain exactly one ${expected}"
done
if cmp -s "$STAGED" "$PROMOTED"; then
    fail "promotion did not create a distinct runtime environment"
fi
grep -Fx 'MAINTENANCE_MODE=true' "$STAGED" >/dev/null \
    || fail "immutable staged environment was modified"

UNSAFE="$TEST_ROOT/unsafe.env"
cp "$PROMOTED" "$UNSAFE"
set_env_value VPN_DRIFT_REPAIR_ENABLED true "$UNSAFE"
if (assert_policy "$UNSAFE" promoted) 2>/dev/null; then
    fail "automatic VPN drift repair was accepted"
fi
cp "$PROMOTED" "$UNSAFE"
set_env_value NOTIFICATIONS_ENABLED true "$UNSAFE"
if (assert_policy "$UNSAFE" promoted) 2>/dev/null; then
    fail "automatic Telegram notifications were accepted"
fi

# The start path must explicitly recreate admin, start a fresh worker before
# reopening bot ingress, and make the billing scheduler the final start command.
: > "$EVENTS_FILE"
LIVE_RELEASE_ENV="$PROMOTED"
compose() {
    {
        printf 'compose'
        printf ' <%s>' "$@"
        printf '\n'
    } >> "$EVENTS_FILE"
}
wait_healthy() {
    printf 'healthy:%s\n' "$1" >> "$EVENTS_FILE"
    printf '%s-id' "$1"
}
wait_stably_running() {
    printf 'stable:%s:%s\n' "$1" "${2:-}" >> "$EVENTS_FILE"
    printf '%s-id' "$1"
}
assert_runtime_policy() {
    printf 'policy:%s\n' "$1" >> "$EVENTS_FILE"
}
assert_container_image() {
    printf 'image:%s:%s\n' "$1" "$2" >> "$EVENTS_FILE"
}
run_admin_smokes() {
    printf 'admin-smoke\n' >> "$EVENTS_FILE"
}
run_read_only_preflights() {
    printf 'read-only-preflights\n' >> "$EVENTS_FILE"
}
scan_service_logs() {
    printf 'logs:%s\n' "$1" >> "$EVENTS_FILE"
}
log() {
    :
}
start_full_services

mapfile -t start_events < <(grep '^compose ' "$EVENTS_FILE")
[[ "${#start_events[@]}" == "4" ]] \
    || fail "start path issued an unexpected number of Compose commands"
[[ "${start_events[0]}" == \
    'compose <--profile> <hub> <up> <-d> <--no-deps> <--pull> <never> <--force-recreate> <admin>' ]]
[[ "${start_events[1]}" == \
    'compose <--profile> <worker> <up> <-d> <--no-deps> <--pull> <never> <--force-recreate> <rq_worker>' ]]
[[ "${start_events[2]}" == \
    'compose <--profile> <bot> <up> <-d> <--no-deps> <--pull> <never> <--force-recreate> <bot>' ]]
[[ "${start_events[3]}" == \
    'compose <--profile> <billing-scheduler> <up> <-d> <--no-deps> <--pull> <never> <--force-recreate> <rq_scheduler>' ]]
if grep -Eq '<(db|redis|migrations)>' "$EVENTS_FILE"; then
    fail "promotion start path operated on a data or migration service"
fi
worker_line="$(grep -n '<rq_worker>' "$EVENTS_FILE" | cut -d: -f1)"
bot_line="$(grep -n '<bot>' "$EVENTS_FILE" | cut -d: -f1)"
scheduler_line="$(grep -n '<rq_scheduler>' "$EVENTS_FILE" | cut -d: -f1)"
[[ "$worker_line" -lt "$bot_line" ]] \
    || fail "bot ingress was reopened before the worker"
[[ "$worker_line" -lt "$scheduler_line" ]] \
    || fail "scheduler was not started after the worker"

# Rollback stops mutation workers first, restores the exact backed-up manifest,
# and only then recreates admin and bot under the fail-closed policy.
: > "$EVENTS_FILE"
DEPLOY_ROOT="$TEST_ROOT/deploy"
mkdir -p "$DEPLOY_ROOT"
LIVE_RELEASE_ENV="$DEPLOY_ROOT/release.env"
BACKUP_ENV="$TEST_ROOT/release.env.before"
IN_PROGRESS_MARKER="$DEPLOY_ROOT/promotion-in-progress"
printf 'state=promoted\n' > "$LIVE_RELEASE_ENV"
printf 'state=staged\n' > "$BACKUP_ENV"
printf 'in-progress\n' > "$IN_PROGRESS_MARKER"
printf '%s\n' '0123456789abcdef0123456789abcdef01234567' \
    > "$DEPLOY_ROOT/current-production"
RELEASE_SHA=0123456789abcdef0123456789abcdef01234567
compose_service_ids() {
    :
}
restore_fail_closed_runtime
grep -Fx 'state=staged' "$LIVE_RELEASE_ENV" >/dev/null \
    || fail "rollback did not restore the release environment backup"
[[ ! -e "$IN_PROGRESS_MARKER" ]] \
    || fail "rollback left the in-progress marker behind"
[[ ! -e "$DEPLOY_ROOT/current-production" ]] \
    || fail "rollback left the current-production marker behind"
mapfile -t rollback_events < <(grep '^compose ' "$EVENTS_FILE")
[[ "${rollback_events[0]}" == \
    'compose <--profile> <hub> <--profile> <bot> <--profile> <worker> <--profile> <billing-scheduler> <stop> <--timeout> <60> <rq_scheduler> <rq_worker> <bot> <admin>' ]]
[[ "${rollback_events[1]}" == \
    'compose <--profile> <hub> <up> <-d> <--no-deps> <--pull> <never> <--force-recreate> <admin>' ]]
[[ "${rollback_events[2]}" == \
    'compose <--profile> <bot> <up> <-d> <--no-deps> <--pull> <never> <--force-recreate> <bot>' ]]
if grep -Eq '<(db|redis|migrations)>' "$EVENTS_FILE"; then
    fail "rollback operated on a data or migration service"
fi

# The workflow has two complete gate checks: before and after environment
# approval. It never rebuilds or uploads code and requires both host markers.
workflow="$REPO_ROOT/.github/workflows/promote-production.yml"
release_workflow="$REPO_ROOT/.github/workflows/release.yml"
ci_workflow="$REPO_ROOT/.github/workflows/ci.yml"
grep -F 'test "$RELEASE_REF" = "refs/heads/main"' "$workflow" >/dev/null
grep -F 'test "$CONFIRMATION" = "PROMOTE_FULL_PRODUCTION"' "$workflow" >/dev/null
[[ "$(grep -Fc 'repos/$REPOSITORY/commits/main' "$workflow")" == "2" ]]
[[ "$(grep -Fc 'actions/workflows/ci.yml/runs?head_sha=$RELEASE_SHA' "$workflow")" == "2" ]]
[[ "$(grep -Fc 'actions/workflows/release.yml/runs?head_sha=$RELEASE_SHA' "$workflow")" == "2" ]]
[[ "$(grep -Fc 'actions/workflows/activate-admin-hub.yml/runs?head_sha=$RELEASE_SHA' "$workflow")" == "2" ]]
grep -F "\$DEPLOY_PATH/current-release" "$workflow" >/dev/null
grep -F "\$DEPLOY_PATH/current-admin-hub" "$workflow" >/dev/null
if grep -Eq 'actions/checkout|docker/build-push|(^|[[:space:]])scp[[:space:]]' "$workflow"; then
    fail "promotion workflow rebuilds or uploads a release"
fi
grep -F 'deploy/promote_full_production.sh' "$release_workflow" >/dev/null
grep -F "promote_full_production.sh' '\$release_dir/promote_full_production.sh'" \
    "$release_workflow" >/dev/null
grep -F 'deploy/promote_full_production.sh' "$ci_workflow" >/dev/null
grep -F 'tests/test_full_production_promotion.sh' "$ci_workflow" >/dev/null

if grep -Eq -- '--insecure|(^|[[:space:]])-k([[:space:]]|$)' \
    "$REPO_ROOT/deploy/promote_full_production.sh"; then
    fail "public HTTPS smoke disables certificate verification"
fi
if grep -Eq -- 'docker compose down|down -v|alembic (upgrade|downgrade)' \
    "$REPO_ROOT/deploy/promote_full_production.sh"; then
    fail "promotion script contains a destructive or migration command"
fi

printf 'full production promotion regression: ok\n'
