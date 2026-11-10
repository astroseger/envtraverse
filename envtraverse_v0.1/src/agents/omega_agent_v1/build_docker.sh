#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
image_name=omega-agent-v1
host_uid=$(id -u)
host_gid=$(id -g)
agent_image="$image_name:$host_uid-$host_gid"

if [[ $# -gt 1 || ( $# -eq 1 && "${1-}" != --if-missing ) ]]; then
    printf 'Usage: %s [--if-missing]\n' "$0" >&2
    exit 2
fi
build_mode=${1-}

exit_if_image_exists() {
    if [[ "$build_mode" == --if-missing ]]; then
        local image_id
        image_id=$(docker image ls --quiet --filter "reference=$agent_image")
        if [[ -n "$image_id" ]]; then
            printf '%s\n' "$agent_image"
            exit 0
        fi
    fi
}

exit_if_image_exists
exec 9>>"/tmp/$image_name.$host_uid-$host_gid.build.lock"
flock 9
exit_if_image_exists

# Resolve the branch for every explicit rebuild so Docker cannot reuse a stale
# cached clone. The Dockerfile fetches this exact public commit.
printf 'Resolving Omega branch chatgpt-subscription...\n' >&2
revision=$(git ls-remote --exit-code https://github.com/astroseger/Omega.git \
    refs/heads/chatgpt-subscription | awk '{print $1}')
if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
    printf 'Error: could not determine the Omega source commit.\n' >&2
    exit 1
fi

context_dir=$(mktemp -d "${TMPDIR:-/tmp}/omega-agent-context.XXXXXXXX")
trap 'rm -rf -- "$context_dir"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p "$context_dir/agents/omega_agent_v1/tests"
cp "$script_dir/constraints.txt" "$context_dir/agents/omega_agent_v1/constraints.txt"
cp "$script_dir/tests/verify_native.py" "$context_dir/agents/omega_agent_v1/tests/verify_native.py"
cp "$script_dir/Dockerfile" "$context_dir/Dockerfile"

printf 'Building native Ubuntu Omega image %s from commit %s...\n' "$agent_image" "$revision" >&2
# Only public build/verification files enter the context, never account/run data.
docker build --build-arg "OMEGA_REVISION=$revision" \
    --build-arg "USER_ID=$host_uid" --build-arg "GROUP_ID=$host_gid" \
    -t "$agent_image" "$context_dir" >&2

printf '%s\n' "$agent_image"
