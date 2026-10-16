#!/usr/bin/env python3
from common_test import run

CONFIG: dict = {
    "version": 1,
    "metadata": {"note": "scenario template"},
    "board": {"n": 4, "m": 4},
    "rng": {"seed": 1111, "algorithm": "py-random-v1"},
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
            "enabled": True,
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

ACTIONS: list[str] = [
    "n", "n", "n", "w", "w", "w", "s", "s", "e", "s", "n",
    "w", "w", "w", "s", "s", "s", "e", "w", "n", "e", "w", "e"
]

if __name__ == "__main__":
    run(CONFIG, ACTIONS)
