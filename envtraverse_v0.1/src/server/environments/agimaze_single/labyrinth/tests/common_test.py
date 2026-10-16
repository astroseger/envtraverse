#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sys


# Allow running directly from repo root.
THIS = Path(__file__).resolve()
sys.path.insert(0, str(THIS.parents[1] / "src"))

from labyrinth.config import config_from_dict
from labyrinth.generator import generate
from labyrinth.engine import Engine
from labyrinth.render import render


def run(CONFIG, ACTIONS) -> None:
    ALIAS = {
        'e': 'right',
        'n': 'up',
        'w': 'left',
        's': 'down'
    }
    ACTIONS = [ALIAS.get(a, a) for a in ACTIONS]
    cfg = config_from_dict(CONFIG)
    gen = generate(cfg)

    eng = Engine(
        gen.board,
        gen.start,
        lang="en",
        require_exit_key_to_leave=gen.goal.require_exit_key_to_leave,
        grenades_enabled=bool(gen.config.systems.grenades.enabled),
        boats_enabled=bool(gen.config.systems.boats.enabled),
    )

    # Helpful header for debugging.
    print(render(gen.board, gen.start))
    print()
    print(
        f"Solvable (generator stats): {gen.stats.solvable}; "
        f"shortest_steps={gen.stats.shortest_steps}; explored={gen.stats.explored_states}"
    )
    print()

    last = None
    for i, a in enumerate(ACTIONS, start=1):
        last = eng.step(a)  # type: ignore[arg-type]
        # Uncomment for step-by-step trace:
        # print(f"{i:03d} {a:>10} -> {last.text}")

        if "exited" in last.obs.events:
            print(f"OK: exited on step {i}")
            return

    # If we get here, script didn't finish the maze.
    print("FAILED: did not exit.")
    print("Final snapshot:", json.dumps(eng.snapshot(), ensure_ascii=False))
    print()
    print("Final map:")
    print(render(gen.board, eng.state))

    if last is not None:
        raise AssertionError(
            f"Did not exit. Last events={last.obs.events}, last observe={last.obs.observe}"
        )

    raise AssertionError(
        "Did not exit (no actions ran). Fill ACTIONS with a winning action sequence."
    )

