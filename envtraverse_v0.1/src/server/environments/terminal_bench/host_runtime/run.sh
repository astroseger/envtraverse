#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
extra=()
for option in "$@"; do
    case "$option" in
        --verbose) extra+=(--verbose) ;;
        -h|--help)
            echo 'Usage: host_runtime/run.sh [--verbose]'
            echo 'Starts the host runtime in the foreground. Refuses existing run/ or run_tb/.'
            exit 0 ;;
        *) echo "Unknown option: $option" >&2; exit 2 ;;
    esac
done
for directory in "$TB_PROJECT_DIR/run" "$TB_PROJECT_DIR/run_tb"; do
    [[ ! -e "$directory" && ! -L "$directory" ]] || { echo "Run folder already exists: $directory" >&2; exit 1; }
done
require_host
catalog_arguments
run_directory="$TB_PROJECT_DIR/run_tb"
umask 077
mkdir "$run_directory"
mkdir "$run_directory/socket" "$run_directory/results"
# A relative bind avoids Linux's Unix-socket pathname length limit. All other
# paths passed to the runtime and its worker processes remain absolute.
cd "$run_directory"
exec "$PYTHON" -u -m terminal_bench.runtime --socket socket/runtime.sock \
    --runs-dir "$TB_PROJECT_DIR/run" --output "$run_directory/results" --log-file "$run_directory/runtime.log" \
    "${CATALOG_ARGUMENTS[@]}" "${extra[@]}"
