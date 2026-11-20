#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"
if (($#)); then
    [[ "$#" == 1 && ( "$1" == --help || "$1" == -h ) ]] || { echo 'Usage: setup.sh' >&2; exit 2; }
    echo 'Build the pinned Minecraft client and observer-server images. Requires Docker Compose.'
    exit 0
fi
require_host
docker build -t minecraft-realtime-server "$MC_PACKAGE_DIR/game/server"
docker build -t minecraft-realtime-client -f "$MC_PACKAGE_DIR/game/client/Dockerfile" "$MC_PACKAGE_DIR/game"
echo 'Real-Time Minecraft images and versioned assets are prepared. Normal runs reuse them; new worlds remain fresh.'
