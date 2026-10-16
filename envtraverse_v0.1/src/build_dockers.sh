#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)

if [[ $# -ne 2 && $# -ne 5 ]]; then
    printf 'Usage: %s ENVIRONMENT AGENT [AGENT_IMAGE SERVER_IMAGE PROXY_IMAGE]\n' "$0" >&2
    exit 2
fi
environment_name=$1
agent_name=$2
if [[ ! "$environment_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || [[ ! -f "$script_dir/server/environments/$environment_name/environment_manager.py" ]]; then
    printf 'Unknown or invalid environment package: %s\n' "$environment_name" >&2
    exit 2
fi

if [[ ! "$agent_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || [[ ! -f "$script_dir/agents/$agent_name/Dockerfile" || ! -f "$script_dir/agents/$agent_name/prepare_run.py" ]]; then
    printf 'Unknown or invalid agent package: %s\n' "$agent_name" >&2
    exit 2
fi

if [[ $# -eq 5 ]]; then
    agent_image=$3
    server_image=$4
    proxy_image=$5
else
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

docker build --build-arg USER_ID="$(id -u)" --build-arg GROUP_ID="$(id -g)" -t "$agent_image" -f "$script_dir/agents/$agent_name/Dockerfile" "$script_dir"
docker build --build-arg USER_ID="$(id -u)" --build-arg GROUP_ID="$(id -g)" --build-arg ENVIRONMENT="$environment_name" -t "$server_image" -f "$script_dir/Dockerfile.server" "$script_dir"
docker build -t "$proxy_image" -f "$script_dir/Dockerfile.proxy" "$script_dir"
