#!/usr/bin/env bash
# Sourced by account entry points. The shell retains the descriptor until exit.

codex_lock_account() {
  local account_path=$1
  local inherited_fd=${2:-}
  local lock_path="$(dirname -- "$account_path")/.$(basename -- "$account_path").codex.lock"
  if [ -n "$inherited_fd" ]; then
    if [[ ! "$inherited_fd" =~ ^[0-9]+$ ]]; then
      echo "Error: --lock-fd must be a non-negative integer" >&2
      return 2
    fi
    CODEX_ACCOUNT_LOCK_FD=$inherited_fd
  else
    if [ -L "$lock_path" ] || { [ -e "$lock_path" ] && [ ! -f "$lock_path" ]; }; then
      echo "Error: account lock must be a regular file: $lock_path" >&2
      return 1
    fi
    local old_umask
    old_umask=$(umask)
    umask 077
    exec {CODEX_ACCOUNT_LOCK_FD}>>"$lock_path"
    umask "$old_umask"
  fi
  python3 "$(dirname -- "${BASH_SOURCE[0]}")/account_locks.py" \
    "$account_path" --kind codex --lock-fd "$CODEX_ACCOUNT_LOCK_FD"
}
