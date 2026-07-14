#!/usr/bin/env bash
# shellcheck disable=SC1090,SC1091,SC2034,SC2317
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EVENTS_FILE="$(mktemp)"
MARKER_FILE="$(mktemp)"
readonly REPO_ROOT EVENTS_FILE MARKER_FILE
trap 'rm -f "$EVENTS_FILE" "$MARKER_FILE"' EXIT

# shellcheck source=../deploy/activate_admin_hub.sh
source "$REPO_ROOT/deploy/activate_admin_hub.sh"

[[ "$POSTGRES_EXPORTER_ROLE" == "vpn_exporter" ]]

fail() {
    printf 'admin hub activation regression: %s\n' "$*" >&2
    exit 1
}

record_compose() {
    {
        printf 'compose'
        printf ' <%s>' "$@"
        printf '\n'
    } >> "$EVENTS_FILE"
}

# The start path must name every allowed service and must suppress dependency
# traversal, so neither the bot nor the migrations one-shot can be started.
: > "$EVENTS_FILE"
compose() {
    record_compose "$@"
}
wait_healthy() {
    printf 'healthy:%s\n' "$1" >> "$EVENTS_FILE"
    printf '%s-id' "$1"
}
log() {
    :
}
start_new_services

grep -Fx \
    'compose <--profile> <monitoring> <pull> <statsd_exporter> <postgres_exporter> <redis_exporter> <prometheus>' \
    "$EVENTS_FILE" >/dev/null
grep -Fx \
    'compose <--profile> <monitoring> <up> <-d> <--no-deps> <statsd_exporter> <postgres_exporter> <redis_exporter> <prometheus>' \
    "$EVENTS_FILE" >/dev/null
grep -Fx \
    'compose <--profile> <hub> <up> <-d> <--no-deps> <--pull> <never> <admin>' \
    "$EVENTS_FILE" >/dev/null
grep -Fx \
    'compose <--profile> <hub> <up> <-d> <--no-deps> <--pull> <never> <admin_frontend> <nginx>' \
    "$EVENTS_FILE" >/dev/null
if grep -Eq '<(bot|migrations|rq_worker|rq_scheduler)>' "$EVENTS_FILE"; then
    fail "start path included a bot, migration, worker, or scheduler service"
fi

# A failed activation stops exactly the new hub/monitoring layer. It must not
# restart an older application image or operate on the running bot.
: > "$EVENTS_FILE"
status=0
if (
    compose() {
        record_compose "$@"
    }
    log() {
        :
    }
    ACTIVATION_STARTED=true
    ACTIVATION_COMMITTED=false
    trap on_exit EXIT
    die "forced activation failure"
) 2>/dev/null; then
    fail "forced activation failure unexpectedly succeeded"
else
    status=$?
fi
[[ "$status" == "1" ]]
grep -Fx \
    'compose <--profile> <hub> <--profile> <monitoring> <stop> <--timeout> <30> <admin> <admin_frontend> <nginx> <statsd_exporter> <postgres_exporter> <redis_exporter> <prometheus>' \
    "$EVENTS_FILE" >/dev/null
if grep -Eq '<(bot|migrations|rq_worker|rq_scheduler)>' "$EVENTS_FILE"; then
    fail "failure cleanup touched a bot, migration, worker, or scheduler service"
fi

# Once the success marker is committed, a later shell error must not tear down
# the accepted hub. The idempotent path performs read-only smokes instead.
: > "$EVENTS_FILE"
status=0
if (
    compose() {
        record_compose "$@"
    }
    ACTIVATION_STARTED=true
    ACTIVATION_COMMITTED=true
    trap on_exit EXIT
    die "forced post-commit failure"
) 2>/dev/null; then
    fail "forced post-commit failure unexpectedly succeeded"
else
    status=$?
fi
[[ "$status" == "1" ]]
[[ ! -s "$EVENTS_FILE" ]]

# The loopback acceptance contract is deliberately small and security-sensitive:
# SPA is present, v1 requires a session, and every legacy API is absent.
: > "$EVENTS_FILE"
smoke_spa_at() {
    printf 'spa:%s:%s\n' "$1" "$2" >> "$EVENTS_FILE"
}
assert_http_status() {
    printf 'http:%s:%s\n' "$1" "$2" >> "$EVENTS_FILE"
}
smoke_loopback_routes
[[ "$(<"$EVENTS_FILE")" == $'spa:http://127.0.0.1:14081/:admin SPA loopback\nhttp:/api/admin/v1/auth/me:401\nhttp:/api/users:404\nhttp:/api/configs:404\nhttp:/api/servers:404' ]]

# Public acceptance uses the configured HTTPS origin for both the cert/SNI
# checked SPA request and its same-origin authenticated API route.
: > "$EVENTS_FILE"
ADMIN_PUBLIC_ORIGIN=https://admin.example.test
assert_url_status() {
    printf 'url:%s:%s:%s\n' "$1" "$2" "$3" >> "$EVENTS_FILE"
}
smoke_public_https
[[ "$(<"$EVENTS_FILE")" == $'spa:https://admin.example.test/:public HTTPS admin SPA\nurl:https://admin.example.test/api/admin/v1/auth/me:401:public HTTPS admin session endpoint' ]]

# Exact markers cannot be reused for another SHA.
release_sha="0123456789abcdef0123456789abcdef01234567"
printf '%s\n' "$release_sha" > "$MARKER_FILE"
assert_exact_marker "$MARKER_FILE" "$release_sha" "test release"
if (assert_exact_marker "$MARKER_FILE" "ffffffffffffffffffffffffffffffffffffffff" "test release") 2>/dev/null; then
    fail "mismatched release marker was accepted"
fi

workflow="$REPO_ROOT/.github/workflows/activate-admin-hub.yml"
release_workflow="$REPO_ROOT/.github/workflows/release.yml"
grep -F "test \"\$RELEASE_REF\" = \"refs/heads/main\"" "$workflow" >/dev/null
grep -F "test \"\$CONFIRMATION\" = \"ACTIVATE_ADMIN_HUB\"" "$workflow" >/dev/null
[[ "$(grep -Fc "repos/\$REPOSITORY/commits/main" "$workflow")" == "2" ]]
[[ "$(grep -Fc "actions/workflows/ci.yml/runs?head_sha=\$RELEASE_SHA" "$workflow")" == "2" ]]
[[ "$(grep -Fc "actions/workflows/release.yml/runs?head_sha=\$RELEASE_SHA" "$workflow")" == "2" ]]
[[ "$(grep -Fc "repos/\$REPOSITORY/commits/main" "$release_workflow")" == "2" ]]
[[ "$(grep -Fc "actions/workflows/ci.yml/runs?head_sha=\$RELEASE_SHA" "$release_workflow")" == "2" ]]
grep -F 'Reconfirm release gates after production approval' "$workflow" >/dev/null
grep -F 'Reconfirm current main and exact successful CI after approval' \
    "$release_workflow" >/dev/null
grep -F "'\$DEPLOY_PATH/current-release'" "$workflow" >/dev/null
if grep -Eq 'actions/checkout|docker/build-push|scp ' "$workflow"; then
    fail "activation workflow rebuilds or uploads a release"
fi
[[ "$(grep -Fc 'deploy/activate_admin_hub.sh' "$release_workflow")" == "1" ]]
grep -F "activate_admin_hub.sh' '\$release_dir/activate_admin_hub.sh'" \
    "$release_workflow" >/dev/null
grep -F 'ADMIN_PUBLIC_ORIGIN=%s' "$release_workflow" >/dev/null
grep -F 'vpn_prometheus_data' "$REPO_ROOT/deploy/activate_admin_hub.sh" >/dev/null
if grep -Eq -- '--insecure|(^|[[:space:]])-k([[:space:]]|$)' \
    "$REPO_ROOT/deploy/activate_admin_hub.sh"; then
    fail "public HTTPS smoke disables certificate verification"
fi

ci_workflow="$REPO_ROOT/.github/workflows/ci.yml"
grep -F \
    'rhysd/actionlint:1.7.7@sha256:887a259a5a534f3c4f36cb02dca341673c6089431057242cdc931e9f133147e9' \
    "$ci_workflow" >/dev/null

if grep -Eq -- '--profile[[:space:]]+bot|[[:space:]]alembic[[:space:]]' \
    "$REPO_ROOT/deploy/activate_admin_hub.sh"; then
    fail "activation script contains a bot-profile or Alembic command"
fi

printf 'admin hub activation regression: ok\n'
