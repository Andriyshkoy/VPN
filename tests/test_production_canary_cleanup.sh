#!/usr/bin/env bash
# shellcheck disable=SC1090,SC1091,SC2034,SC2317
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EVENTS_FILE="$(mktemp)"
readonly REPO_ROOT EVENTS_FILE
trap 'rm -f "$EVENTS_FILE"' EXIT

# shellcheck source=../deploy/production_canary.sh
source "$REPO_ROOT/deploy/production_canary.sh"

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
