"""Stable RNG stream derivation.

We want:
- one master seed
- deterministic per-subsystem streams
- adding a new subsystem should not perturb old ones

We do: seed_i = SHA256(f"{master_seed}:{label}") mod 2**32
and feed it to random.Random.

Note: this intentionally does not depend on Python's hash().
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class RNGStreams:
    master_seed: int

    def stream(self, label: str) -> random.Random:
        h = hashlib.sha256(f"{self.master_seed}:{label}".encode("utf-8")).digest()
        seed32 = int.from_bytes(h[:4], "big", signed=False)
        return random.Random(seed32)
