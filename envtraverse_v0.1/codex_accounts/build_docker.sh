#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
CODEX_ACCOUNTS_IMAGE=codex-accounts-image-unified-agent-v1-0

# On success, stdout contains only the image name; diagnostics go to stderr.

if [[ $# -gt 1 || ( $# -eq 1 && "${1-}" != --if-missing ) ]]; then
    printf 'Usage: %s [--if-missing]\n' "$0" >&2
    exit 2
fi
build_mode=${1-}

exit_if_image_exists() {
    if [[ "$build_mode" == --if-missing ]]; then
        local image_id
        # An empty successful result means missing; Docker errors must stop us.
        image_id=$(docker image ls --quiet --filter "reference=$CODEX_ACCOUNTS_IMAGE:latest")
        if [[ -n "$image_id" ]]; then
            printf '%s\n' "$CODEX_ACCOUNTS_IMAGE"
            exit 0
        fi
    fi
}

exit_if_image_exists

# All experiment copies and manual rebuilds use the same lock. Leave the lock
# file in place: removing it could let another process lock a different inode.
exec 9>>"/tmp/$CODEX_ACCOUNTS_IMAGE.build.lock"
flock 9

# Another caller may have finished building while we were waiting for the lock.
exit_if_image_exists

printf 'Building account-tools image %s...\n' "$CODEX_ACCOUNTS_IMAGE" >&2

# An empty build context keeps account folders and credentials out of Docker builds.
docker build --build-arg USER_ID="$(id -u)" --build-arg GROUP_ID="$(id -g)" \
    -t "$CODEX_ACCOUNTS_IMAGE" - < "$script_dir/Dockerfile" >&2

printf '%s\n' "$CODEX_ACCOUNTS_IMAGE"
