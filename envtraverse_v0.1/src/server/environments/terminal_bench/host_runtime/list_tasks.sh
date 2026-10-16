#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
catalog_arguments
exec "$PYTHON" -m terminal_bench.datasets list "${CATALOG_ARGUMENTS[@]}" "$@"
