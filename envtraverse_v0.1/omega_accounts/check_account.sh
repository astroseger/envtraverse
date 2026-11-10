#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: %s <account-dir> [--model MODEL] [--timeout SECONDS]\n' "$0"
}

account_dir=
model=
timeout_seconds=120
while (($#)); do
  case "$1" in
    --model)
      if [[ $# -lt 2 || -z "$2" || "$2" == --* ]]; then usage >&2; exit 2; fi
      model=$2
      shift
      ;;
    --timeout)
      if [[ $# -lt 2 || ! "$2" =~ ^[0-9]*[1-9][0-9]*$ ]]; then
        printf 'Error: --timeout must be a positive integer in seconds.\n' >&2
        exit 2
      fi
      timeout_seconds=$2
      shift
      ;;
    -h|--help) usage; exit 0 ;;
    --)
      shift
      if [[ $# -ne 1 || -n "$account_dir" ]]; then usage >&2; exit 2; fi
      account_dir=$1
      break
      ;;
    -*) usage >&2; exit 2 ;;
    *)
      if [[ -n "$account_dir" ]]; then usage >&2; exit 2; fi
      account_dir=$1
      ;;
  esac
  shift
done
if [[ -z "$account_dir" ]]; then usage >&2; exit 2; fi
case "$account_dir" in
  *','*|*'"'*|*$'\n'*|*$'\r'*)
    printf 'Error: account paths cannot contain commas, double quotes, or line breaks.\n' >&2
    exit 2
    ;;
esac
if [[ ! -d "$account_dir" ]]; then
  printf 'Error: account directory does not exist: %s\n' "$account_dir" >&2
  exit 1
fi
account_dir_abs=$(cd -- "$account_dir" && pwd -P)
case "$account_dir_abs" in
  *','*|*'"'*|*$'\n'*|*$'\r'*)
    printf 'Error: the resolved account path cannot contain commas, double quotes, or line breaks.\n' >&2
    exit 2
    ;;
esac
if [[ ! -O "$account_dir_abs" || ! -r "$account_dir_abs" || ! -w "$account_dir_abs" || ! -x "$account_dir_abs" ]]; then
  printf 'Error: account directory must be owned by you and readable/writable: %s\n' "$account_dir_abs" >&2
  exit 1
fi
auth_file="$account_dir_abs/chatgpt.json"
if [[ ! -f "$auth_file" || -L "$auth_file" || ! -r "$auth_file" || ! -O "$auth_file" ]]; then
  printf 'Error: expected a readable, regular chatgpt.json owned by you in %s\n' "$account_dir_abs" >&2
  exit 1
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
image=$(bash "$script_dir/build_docker.sh" --if-missing)
check_options=(--timeout "$timeout_seconds")
if [[ -n "$model" ]]; then check_options+=(--model "$model"); fi

# Omega's check command enforces its deadline inside this container. The auth
# directory stays writable so token refresh and its lock survive container exit.
if docker run --rm --pull=never --init \
    --mount "type=bind,source=$account_dir_abs,target=/home/user/.omega_auth" \
    "$image" python3 /PeTTa/repos/Omega/scripts/chatgpt-auth check "${check_options[@]}"; then
  printf 'Account OK: %s\n' "$account_dir_abs"
else
  status=$?
  printf 'Account check failed (exit %s): %s\n' "$status" "$account_dir_abs" >&2
  exit "$status"
fi
