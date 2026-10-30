#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
extra=()
run_root="$TB_PROJECT_DIR"
while (($#)); do
    case "$1" in
        --verbose) extra+=(--verbose); shift ;;
        --run-root)
            [[ $# -ge 2 && -n "$2" && "$2" != -* ]] || { echo 'Missing value for --run-root' >&2; exit 2; }
            run_root=$2
            shift 2 ;;
        -h|--help)
            echo 'Usage: host_runtime/run.sh [--run-root PATH] [--verbose]'
            echo 'Starts the host runtime in the foreground. Defaults to the project directory.'
            echo 'Refuses existing run/ or run_tb/ in the selected run root.'
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done
run_root=$(realpath -m -- "$run_root")
for directory in "$run_root/run" "$run_root/run_tb"; do
    [[ ! -e "$directory" && ! -L "$directory" ]] || { echo "Run folder already exists: $directory" >&2; exit 1; }
done
require_host
catalog_arguments
run_directory="$run_root/run_tb"
umask 077
mkdir -p "$run_root"
mkdir "$run_directory"
mkdir "$run_directory/socket" "$run_directory/results"
# A relative bind avoids Linux's Unix-socket pathname length limit. All other
# paths passed to the runtime and its worker processes remain absolute.
cd "$run_directory"
exec "$PYTHON" -u -m terminal_bench.runtime --socket socket/runtime.sock \
    --runs-dir "$run_root/run" --output "$run_directory/results" --log-file "$run_directory/runtime.log" \
    "${CATALOG_ARGUMENTS[@]}" "${extra[@]}"
