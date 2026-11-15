#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
version=2.3.0
image="opencode-chatgpt-accounts:${version}-uid$(id -u)-gid$(id -g)"

if [[ $# -gt 1 || ( $# -eq 1 && "${1-}" != --if-missing ) ]]; then
    printf 'Usage: %s [--if-missing]\n' "$0" >&2
    exit 2
fi
build_mode=${1-}

exit_if_image_exists() {
    if [[ "$build_mode" == --if-missing ]]; then
        local image_id
        # A Docker error must not be mistaken for an absent image.
        image_id=$(docker image ls --quiet --filter "reference=$image")
        if [[ -n "$image_id" ]]; then
            printf '%s\n' "$image"
            exit 0
        fi
    fi
}

exit_if_image_exists
# Keep the lock file: unlinking it would allow callers to lock different inodes.
exec 9>>"/tmp/${image//:/-}.build.lock"
flock 9
exit_if_image_exists

printf 'Building OpenCode account-tools image %s...\n' "$image" >&2
# Docker receives only this Dockerfile, never account directories or credentials.
docker build --build-arg "OPENCODE_VERSION=$version" \
    --build-arg "USER_ID=$(id -u)" --build-arg "GROUP_ID=$(id -g)" \
    -t "$image" - < "$script_dir/Dockerfile" >&2
printf '%s\n' "$image"
