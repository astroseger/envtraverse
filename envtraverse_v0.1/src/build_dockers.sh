#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)

usage() {
    printf 'Usage: %s ENVIRONMENT AGENT [AGENT_IMAGE SERVER_IMAGE PROXY_IMAGE] [--agent-build-arg NAME=VALUE ...]\n' "$0" >&2
    exit 2
}

[[ $# -ge 2 ]] || usage
environment_name=$1
agent_name=$2
shift 2
if [[ ! "$environment_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || [[ ! -f "$script_dir/server/environments/$environment_name/environment_manager.py" ]]; then
    printf 'Unknown or invalid environment package: %s\n' "$environment_name" >&2
    exit 2
fi

if [[ ! "$agent_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || [[ ! -f "$script_dir/agents/$agent_name/Dockerfile" || ! -f "$script_dir/agents/$agent_name/prepare_run.py" ]]; then
    printf 'Unknown or invalid agent package: %s\n' "$agent_name" >&2
    exit 2
fi

image_tags_supplied=false
if [[ $# -gt 0 && "$1" != --agent-build-arg ]]; then
    [[ $# -ge 3 ]] || usage
    agent_image=$1
    server_image=$2
    proxy_image=$3
    image_tags_supplied=true
    shift 3
fi

agent_build_args=()
declare -A supplied_args=()
while [[ $# -gt 0 ]]; do
    if [[ $# -lt 2 ]] || [[ "$1" != --agent-build-arg || "$2" != *=* ]]; then
        usage
    fi
    name=${2%%=*}
    if [[ ! "$name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        printf 'Invalid agent Docker build argument name: %s\n' "$name" >&2
        exit 2
    fi
    if [[ "$name" == USER_ID || "$name" == GROUP_ID ]]; then
        printf 'Agent Docker build argument %s is managed by the controller\n' "$name" >&2
        exit 2
    fi
    if [[ -n "${supplied_args[$name]+present}" ]]; then
        printf 'Duplicate agent Docker build argument: %s\n' "$name" >&2
        exit 2
    fi
    if ! grep -qE "^[[:blank:]]*[aA][rR][gG][[:blank:]]+$name([[:space:]=]|$)" "$script_dir/agents/$agent_name/Dockerfile"; then
        printf '%s/Dockerfile does not declare ARG %s\n' "$agent_name" "$name" >&2
        exit 2
    fi
    supplied_args[$name]=1
    agent_build_args+=(--build-arg "$2")
    shift 2
done

if [[ "$image_tags_supplied" == false ]]; then
    run_id=$(python3 -c 'from uuid import uuid4; print(uuid4().hex)')
    agent_image="unified-agent:run-$run_id"
    server_image="unified-server:run-$run_id"
    proxy_image="unified-proxy:run-$run_id"
fi

# Never replace tags belonging to an existing build, even for manual invocation.
for image in "$agent_image" "$server_image" "$proxy_image"; do
    if [[ ! "$image" =~ ^[a-z0-9][a-z0-9._-]*:run-[0-9a-f]{32}$ ]]; then
        printf 'Expected a unique image tag NAME:run-<32-hex-digit-uuid>: %s\n' "$image" >&2
        exit 2
    fi
done
if [[ "$agent_image" == "$server_image" || "$agent_image" == "$proxy_image" || "$server_image" == "$proxy_image" ]]; then
    printf 'Image tags must be distinct\n' >&2
    exit 2
fi
for image in "$agent_image" "$server_image" "$proxy_image"; do
    existing_image=$(docker image ls --quiet --filter "reference=$image")
    if [[ -n "$existing_image" ]]; then
        printf 'Refusing to replace existing image tag: %s\n' "$image" >&2
        exit 2
    fi
done
printf 'Run images: agent=%s server=%s proxy=%s\n' "$agent_image" "$server_image" "$proxy_image" >&2
printf 'After stopping their containers, remove with: docker image rm --no-prune %s %s %s\n' "$agent_image" "$server_image" "$proxy_image" >&2

docker build --build-arg USER_ID="$(id -u)" --build-arg GROUP_ID="$(id -g)" "${agent_build_args[@]}" -t "$agent_image" -f "$script_dir/agents/$agent_name/Dockerfile" "$script_dir"
docker build --build-arg USER_ID="$(id -u)" --build-arg GROUP_ID="$(id -g)" --build-arg ENVIRONMENT="$environment_name" -t "$server_image" -f "$script_dir/Dockerfile.server" "$script_dir"
docker build -t "$proxy_image" -f "$script_dir/Dockerfile.proxy" "$script_dir"
