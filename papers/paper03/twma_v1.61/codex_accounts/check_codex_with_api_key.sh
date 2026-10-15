#!/usr/bin/env bash
set -euo pipefail

if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "Error: OPENAI_API_KEY is not set" >&2
  exit 1
fi

CODEX_ACCOUNTS_IMAGE=$(
  bash "$(dirname "${BASH_SOURCE[0]}")/build_docker.sh" --if-missing
)

exec_output=$(
  docker run --rm --pull=never \
    -e CODEX_API_KEY="$OPENAI_API_KEY" \
    "$CODEX_ACCOUNTS_IMAGE" \
    codex exec --skip-git-repo-check "Respond with exactly: PING" 2>&1
)

if ! printf '%s\n' "$exec_output" | grep -q '^PING$'; then
  echo "Error: codex exec did not return PING" >&2
  printf '%s\n' "$exec_output" >&2
  exit 1
fi

echo "API key account OK"
