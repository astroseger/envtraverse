#!/usr/bin/env python3

"""Scenario-style regression test template.

Goal:
- Fix a config (incl. seed) so generation is reproducible.
- Define a deterministic action script.
- Assert the script reaches victory (event 'exited').

This checks both:
- generation stability for the given config
- runtime command semantics (engine.step)

Run:
  python3 projects/labyrinth/tests/test_scenario_template.py
"""

from common_test import run


# -----------------
# CONFIG (fill in)

CONFIG: dict = {
    "version": 1,
    "metadata": {"note": "scenario template"},
    "board": {"n": 3, "m": 3},
    "rng": {"seed": 12345, "algorithm": "py-random-v1"},
    "strategy": {"name": "basic-v1", "params": {}},
    "systems": {
        "walls": {
            "enabled": True,
            "strategy": "degree-v1",
            "target_mean_degree": 1.2,
            "nonriver_engine_enabled": True,
            "river_enabled": True,
            "target_mean_river_walls": 1.0,
        },
        "keys": True,
        "treasure": True,
        "exit_key": False,
        "rivers": {
            "enabled": False,
            "strategy": "linear-v1",
            "density": 0.30,
            "mean_len": 1.40,
            "scatter": 0.25,
            "straight_bias": 2.0,
            "adj_penalty": 0.40,
            "source_dist_power": 3.0,
            "allow_adjacent": True,
        },
        "pits_cfg": {
            "enabled": False,
            "strategy": "cycles-v1",
            "cycles": 2,
            "cycle_len": 3,
            "allow_on_river": False,
        },
        "grenades": {
            "enabled": False,
            "strategy": "seal-cell-v1",
            "extra_grenades": 1,
            "max_walls_to_add": 3,
        },
        "boats": {
            "enabled": False,
            "strategy": "river-exit-v1",
        },
    },
}


# -----------------
# ACTION SCRIPT
# Valid actions: up/down/left/right, n/s/w/e
# If grenades are enabled: bomb_up/bomb_down/bomb_left/bomb_right.
ACTIONS: list[str] = [
    "e", "s", "e", "w", "n", "n", "e", "w", "n", "s", "w", "s", "w"
]

if __name__ == "__main__":
    run(CONFIG, ACTIONS)
