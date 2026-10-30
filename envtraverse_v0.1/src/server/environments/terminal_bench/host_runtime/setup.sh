#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
benchmarks=2.1
tb21_ref=""
tb3_ref=""
tb4_ref=""
tbscience_ref=""
while (($#)); do
    case "$1" in
        -h|--help)
            echo 'Usage: host_runtime/setup.sh [--benchmarks 2.1|3.0,4.0|science-0.1|all] [--tb21-ref REF] [--tb3-ref REF] [--tb4-ref REF] [--tbscience-ref REF]'
            echo 'Installs Harbor 0.23.0 and downloads only Terminal-Bench 2.1 by default.'
            exit 0 ;;
        --benchmarks|--tb21-ref|--tb3-ref|--tb4-ref|--tbscience-ref)
            [[ $# -ge 2 && -n "$2" && "$2" != -* ]] || { echo "Missing value for $1" >&2; exit 2; }
            case "$1" in
                --benchmarks) benchmarks="$2" ;;
                --tb21-ref) tb21_ref="$2" ;;
                --tb3-ref) tb3_ref="$2" ;;
                --tb4-ref) tb4_ref="$2" ;;
                --tbscience-ref) tbscience_ref="$2" ;;
            esac
            shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done
[[ "$benchmarks" != all ]] || benchmarks=2.1,3.0,4.0,science-0.1
[[ -n "$benchmarks" && "$benchmarks" != ,* && "$benchmarks" != *, && "$benchmarks" != *,,* ]] || {
    echo 'Invalid --benchmarks list' >&2; exit 2;
}
IFS=',' read -r -a versions <<< "$benchmarks"
seen=,
for version in "${versions[@]}"; do
    case "$version" in 2.1|3.0|4.0|science-0.1) ;; *) echo "Unsupported version: $version" >&2; exit 2 ;; esac
    [[ "$seen" != *",$version,"* ]] || { echo "Duplicate version: $version" >&2; exit 2; }
    seen+="$version,"
done
for pair in "2.1:$tb21_ref" "3.0:$tb3_ref" "4.0:$tb4_ref" "science-0.1:$tbscience_ref"; do
    [[ -z "${pair#*:}" || "$seen" == *",${pair%%:*},"* ]] || {
        echo "Ref provided for unselected benchmark: ${pair%%:*}" >&2; exit 2;
    }
done
bootstrap="${BOOTSTRAP_PYTHON:-python3}"
command -v git >/dev/null || { echo 'Git is required.' >&2; exit 1; }
"$bootstrap" -c 'import sys; assert sys.version_info >= (3,12), "Python 3.12+ is required"'
[[ -x "$TB_VENV/bin/python" ]] || "$bootstrap" -m venv "$TB_VENV"
"$TB_VENV/bin/python" -m pip install -r "$TB_HOST_DIR/requirements.txt"
PYTHON="$TB_VENV/bin/python"
"$PYTHON" -c 'from terminal_bench.harbor_runner import check_harbor; print("Harbor", check_harbor())'
for version in "${versions[@]}"; do
    case "$version" in
        2.1) destination="$TB21_ROOT"; ref="$tb21_ref"; option=--tb21-root; label=tb21 ;;
        3.0) destination="$TB3_ROOT"; ref="$tb3_ref"; option=--tb3-root; label=tb3 ;;
        4.0) destination="$TB4_ROOT"; ref="$tb4_ref"; option=--tb4-root; label=tb4 ;;
        science-0.1) destination="$TBSCIENCE_ROOT"; ref="$tbscience_ref"; option=--tbscience-root; label=tbscience ;;
    esac
    extra=()
    [[ -z "$ref" ]] || extra+=(--ref "$ref")
    "$PYTHON" -m terminal_bench.datasets fetch --benchmark "terminal-bench-$version" \
        --destination "$destination" "${extra[@]}"
    "$PYTHON" -m terminal_bench.datasets list "$option" "$destination" \
        --benchmark "terminal-bench-$version" --format yaml > "$TB_HOST_DIR/$label-games.generated.yaml"
done
echo 'Setup complete. Task images are prepared lazily on start.'
