#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ] && { [ "$#" -ne 3 ] || [ "${2:-}" != "--lock-fd" ]; }; then
  echo "Usage: $0 <codex-home-path> [--lock-fd FD]" >&2
  exit 2
fi

codex_home=$1
inherited_lock_fd=${3:-}
if [ "$#" -eq 3 ] && [[ ! "$inherited_lock_fd" =~ ^[0-9]+$ ]]; then
  echo "Error: --lock-fd must be a non-negative integer" >&2
  exit 2
fi

if [ -L "$codex_home" ] || [ ! -d "$codex_home" ]; then
  echo "Error: folder does not exist: $codex_home" >&2
  exit 1
fi

codex_home_abs=$(cd "$codex_home" && pwd -P)
source "$(dirname -- "${BASH_SOURCE[0]}")/account_lock.sh"
codex_lock_account "$codex_home_abs" "$inherited_lock_fd"

CODEX_ACCOUNTS_IMAGE=$(
  bash "$(dirname "${BASH_SOURCE[0]}")/build_docker.sh" --if-missing
)

exec python3 "$(dirname -- "${BASH_SOURCE[0]}")/check_account.py" \
  "$codex_home" "$CODEX_ACCOUNTS_IMAGE" --lock-fd "$CODEX_ACCOUNT_LOCK_FD"
