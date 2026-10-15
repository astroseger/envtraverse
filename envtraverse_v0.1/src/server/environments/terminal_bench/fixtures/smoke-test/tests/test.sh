#!/bin/bash
set -eu
mkdir -p /logs/verifier
if python3 - <<'PY'
from pathlib import Path
assert Path('/app/answer.txt').read_bytes() == b'hello from terminal bench\n'
PY
then
    printf '1\n' > /logs/verifier/reward.txt
else
    printf '0\n' > /logs/verifier/reward.txt
fi
