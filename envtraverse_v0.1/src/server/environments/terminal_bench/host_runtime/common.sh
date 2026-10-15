#!/usr/bin/env bash
# Absolute locations keep all entry points independent of the caller's CWD.
TB_HOST_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TB_PACKAGE_DIR="$(cd -- "$TB_HOST_DIR/.." && pwd)"
TB_PROJECT_DIR="$(cd -- "$TB_PACKAGE_DIR/../../../.." && pwd)"
TB_VENV="${TB_VENV:-$TB_HOST_DIR/.venv}"
PYTHON="${PYTHON:-$TB_VENV/bin/python}"
TB21_ROOT="${TB21_ROOT:-$TB_HOST_DIR/.tb-data/terminal-bench-2-1}"
TB3_ROOT="${TB3_ROOT:-$TB_HOST_DIR/.tb-data/terminal-bench-3-0}"
TB4_ROOT="${TB4_ROOT:-$TB_HOST_DIR/.tb-data/terminal-bench-4-0}"
export PYTHONPATH="$TB_PACKAGE_DIR/..:$TB_HOST_DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1

require_host() {
    [[ -x "$PYTHON" ]] || { echo 'Run host_runtime/setup.sh first.' >&2; return 1; }
    command -v docker >/dev/null || { echo 'Docker is required.' >&2; return 1; }
    docker info >/dev/null
    docker compose version >/dev/null
    "$PYTHON" -c 'from terminal_bench.harbor_runner import check_harbor; check_harbor()'
}

catalog_arguments() {
    CATALOG_ARGUMENTS=()
    [[ ! -d "$TB21_ROOT/tasks" ]] || CATALOG_ARGUMENTS+=(--tb21-root "$TB21_ROOT")
    [[ ! -d "$TB3_ROOT/tasks" ]] || CATALOG_ARGUMENTS+=(--tb3-root "$TB3_ROOT")
    [[ ! -d "$TB4_ROOT/tasks" ]] || CATALOG_ARGUMENTS+=(--tb4-root "$TB4_ROOT")
}
