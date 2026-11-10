#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
image_name=omega-accounts-image-unified-agent-v1-0
host_uid=$(id -u)
host_gid=$(id -g)
accounts_image="$image_name:$host_uid-$host_gid"

if [[ $# -gt 1 || ( $# -eq 1 && "${1-}" != --if-missing ) ]]; then
    printf 'Usage: %s [--if-missing]\n' "$0" >&2
    exit 2
fi
build_mode=${1-}

exit_if_image_exists() {
    if [[ "$build_mode" == --if-missing ]]; then
        local image_id
        # Missing images produce empty output; Docker daemon errors must fail.
        image_id=$(docker image ls --quiet --filter "reference=$accounts_image")
        if [[ -n "$image_id" ]]; then
            printf '%s\n' "$accounts_image"
            exit 0
        fi
    fi
}

exit_if_image_exists

# Shared across experiment copies. Keep the inode after releasing the lock.
exec 9>>"/tmp/$image_name.$host_uid-$host_gid.build.lock"
flock 9
exit_if_image_exists

source_dir=$(mktemp -d "${TMPDIR:-/tmp}/omega-accounts-source.XXXXXXXX")
trap 'rm -rf -- "$source_dir"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

printf 'Fetching Omega branch chatgpt-subscription...\n' >&2
git clone --depth 1 --single-branch --branch chatgpt-subscription \
    https://github.com/astroseger/Omega.git "$source_dir/source" >&2
revision=$(git -C "$source_dir/source" rev-parse HEAD)
if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
    printf 'Error: could not determine the Omega source commit.\n' >&2
    exit 1
fi
base_image="omega-subscription-base:$revision"

printf 'Building full Omega installation from commit %s...\n' "$revision" >&2
# Only the public repository checkout enters this context, never account folders.
docker build --label "org.opencontainers.image.revision=$revision" \
    -t "$base_image" "$source_dir/source" >&2

printf 'Building account-tools image %s...\n' "$accounts_image" >&2
# The wrapper needs no local files; credentials never enter its build context.
docker build --build-arg "OMEGA_BASE_IMAGE=$base_image" \
    --build-arg "OMEGA_REVISION=$revision" \
    --build-arg "USER_ID=$host_uid" --build-arg "GROUP_ID=$host_gid" \
    -t "$accounts_image" - < "$script_dir/Dockerfile" >&2

# Callers capture stdout. All progress and errors above belong on stderr.
printf '%s\n' "$accounts_image"
