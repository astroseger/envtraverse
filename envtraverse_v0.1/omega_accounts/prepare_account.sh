#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: %s [--resume] <account-dir>\n' "$0"
}

resume=0
account_dir=
while (($#)); do
  case "$1" in
    --resume) resume=1 ;;
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

if ((resume)); then
  if [[ ! -d "$account_dir" ]]; then
    printf 'Error: --resume requires an existing account directory: %s\n' "$account_dir" >&2
    exit 1
  fi
  if [[ ! -O "$account_dir" || ! -r "$account_dir" || ! -w "$account_dir" || ! -x "$account_dir" ]]; then
    printf 'Error: account directory must be owned by you and readable/writable: %s\n' "$account_dir" >&2
    exit 1
  fi
elif [[ -e "$account_dir" || -L "$account_dir" ]]; then
  printf 'Error: path already exists: %s (use --resume to continue its login)\n' "$account_dir" >&2
  exit 1
fi

# Canonicalize before constructing Docker's comma-separated mount argument.
account_dir_abs=$(realpath -m -- "$account_dir")
case "$account_dir_abs" in
  *','*|*'"'*|*$'\n'*|*$'\r'*)
    printf 'Error: the resolved account path cannot contain commas, double quotes, or line breaks.\n' >&2
    exit 2
    ;;
esac
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
image=$(bash "$script_dir/build_docker.sh" --if-missing)

# A failed image build leaves no new account directory. A failed login keeps
# its registration so --resume can reuse the same client and host identity.
if ((!resume)); then
  umask 077
  mkdir -p -- "$(dirname -- "$account_dir_abs")"
  mkdir -m 0700 -- "$account_dir_abs"
fi
chmod 0700 -- "$account_dir_abs"
printf 'Open the login URL below in a browser on this Linux host.\n' >&2
if docker run --rm --pull=never --init --network host \
    --mount "type=bind,source=$account_dir_abs,target=/home/user/.omega_auth" \
    "$image" python3 /PeTTa/repos/Omega/scripts/chatgpt-auth login --no-browser; then
  printf 'Account prepared: %s\n' "$account_dir_abs"
else
  status=$?
  printf 'Login failed (exit %s). Retry with: %q --resume %q\n' "$status" "$0" "$account_dir_abs" >&2
  exit "$status"
fi
