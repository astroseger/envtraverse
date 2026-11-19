#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
    exec "$PYTHON" -m life_minecraft.runtime --help
fi
require_host
exec "$PYTHON" -u -m life_minecraft.runtime "$@"
