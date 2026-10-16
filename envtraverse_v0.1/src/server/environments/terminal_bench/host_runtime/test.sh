#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
exec "$PYTHON" -m unittest discover -s "$TB_HOST_DIR/tests" -t "$TB_HOST_DIR" "$@"
