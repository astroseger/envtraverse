#!/usr/bin/env bash
MC_HOST_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
MC_PACKAGE_DIR="$(cd -- "$MC_HOST_DIR/.." && pwd)"
PYTHON="${PYTHON:-python3}"
export PYTHONPATH="$MC_PACKAGE_DIR/..${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1

require_host() {
    "$PYTHON" -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ is required"'
    command -v docker >/dev/null || { echo 'Docker is required.' >&2; return 1; }
    docker info >/dev/null
    docker compose version >/dev/null
}
