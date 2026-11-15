#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 <codex-home-path>" >&2
  exit 2
fi

codex_home=$1

if [ -e "$codex_home" ] || [ -L "$codex_home" ]; then
  echo "Error: path already exists: $codex_home" >&2
  exit 1
fi

codex_home_abs=$(realpath -m -- "$codex_home")
mkdir -p -- "$(dirname -- "$codex_home_abs")"
source "$(dirname -- "${BASH_SOURCE[0]}")/account_lock.sh"
codex_lock_account "$codex_home_abs"
# Another cooperating operation cannot create this path while we hold its lock.
if [ -e "$codex_home_abs" ]; then
  echo "Error: path already exists: $codex_home" >&2
  exit 1
fi

CODEX_ACCOUNTS_IMAGE=$(
  bash "$(dirname "${BASH_SOURCE[0]}")/build_docker.sh" --if-missing
)

mkdir -p "$codex_home_abs"
printf '%s\n' 'web_search = "disabled"' > "$codex_home_abs/config.toml"

docker run --rm --pull=never -it -v "$codex_home_abs:/home/user/.codex" --network host "$CODEX_ACCOUNTS_IMAGE" codex login --device-auth
