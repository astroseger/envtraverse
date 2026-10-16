"""Loader that uses saved generation if available, otherwise generates.

Two supported inputs:
1) A plain config dict (no saved generation) -> we run generate().
2) A bundle dict (format labyrinth.bundle.v1) with a 'generated' section -> we load it.

This is intended for benchmark packs and server startup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .config import config_from_dict
from .generator import Generated, generate
from .io import bundle_has_generated, load_bundle


@dataclass
class Loaded:
    config: Dict[str, Any]
    generated: Generated
    source: str  # "generated" | "loaded"


def load_or_generate(d: Dict[str, Any]) -> Loaded:
    # bundle path
    if d.get("format") == "labyrinth.bundle.v1" and bundle_has_generated(d):
        cfg_dict, board, start, goal = load_bundle(d)

        # stats can be recomputed later; keep minimal
        from .generator import SolveStats

        gen = Generated(
            config=config_from_dict(cfg_dict),
            board=board,
            start=start,
            goal=goal,
            stats=SolveStats(solvable=True, shortest_steps=None, explored_states=0),
        )
        return Loaded(config=cfg_dict, generated=gen, source="loaded")

    # plain config
    cfg = config_from_dict(d)
    gen = generate(cfg)
    return Loaded(config=d, generated=gen, source="generated")
