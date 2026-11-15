#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: %s [--ignore <directory_name>]... <account_name>\n' "$0"
  printf 'Hidden directories are always ignored. Repeat --ignore to exclude more names.\n'
  printf 'Ignored names match directory basenames at any depth using find -name patterns.\n'
  printf 'If the account directory is already here, no action is taken.\n'
}

ignore_names=('.*')
account_args=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --ignore)
      if [ "$#" -lt 2 ] || [ -z "$2" ] || [[ "$2" == --* ]]; then
        echo "Error: --ignore requires a directory name (use --ignore=NAME for names starting with --)" >&2
        usage >&2
        exit 2
      fi
      ignore_names+=("$2")
      shift 2
      ;;
    --ignore=*)
      ignore_name=${1#--ignore=}
      if [ -z "$ignore_name" ]; then
        echo "Error: --ignore requires a directory name" >&2
        exit 2
      fi
      ignore_names+=("$ignore_name")
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      account_args+=("$@")
      break
      ;;
    -*)
      echo "Error: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      account_args+=("$1")
      shift
      ;;
  esac
done

if [ "${#account_args[@]}" -ne 1 ]; then
  usage >&2
  exit 2
fi

account_name=${account_args[0]}

if [ -z "$account_name" ]; then
  echo "Error: account name must not be empty" >&2
  exit 2
fi

if [ -d "./$account_name" ]; then
  printf 'Account folder already in place: ./%s\n' "$account_name"
  exit 0
fi

prune_names=(-name "${ignore_names[0]}")
for ignore_name in "${ignore_names[@]:1}"; do
  prune_names+=(-o -name "$ignore_name")
done

mapfile -t matches < <(
  find ~/ -type d \( "${prune_names[@]}" \) -prune -o \
    -type d -name "$account_name" -print
)

if [ "${#matches[@]}" -eq 0 ]; then
  echo "Error: account folder not found under ~/ with name: $account_name" >&2
  exit 1
fi

if [ "${#matches[@]}" -gt 1 ]; then
  echo "Error: multiple account folders found for name: $account_name" >&2
  printf '%s\n' "${matches[@]}" >&2
  exit 1
fi

source_path=${matches[0]}

if [ ! -d "$source_path" ]; then
  echo "Error: found path is not a directory: $source_path" >&2
  exit 1
fi

source_path=$(cd "$source_path" && pwd -P)
destination_path="$(pwd -P)/$(basename -- "$source_path")"
if [ "$source_path" = "$destination_path" ]; then
  printf 'Account folder already in place: %s\n' "$destination_path"
  exit 0
fi

# Retain both sibling locks across the move, including across filesystems.
source "$(dirname -- "${BASH_SOURCE[0]}")/account_lock.sh"
codex_lock_account "$source_path"
source_lock_fd=$CODEX_ACCOUNT_LOCK_FD
codex_lock_account "$destination_path"

if [ ! -d "$source_path" ]; then
  echo "Error: source account disappeared: $source_path" >&2
  exit 1
fi
if [ -e "$destination_path" ] || [ -L "$destination_path" ]; then
  echo "Error: destination already exists: $destination_path" >&2
  exit 1
fi

mv -- "$source_path" "$destination_path"
