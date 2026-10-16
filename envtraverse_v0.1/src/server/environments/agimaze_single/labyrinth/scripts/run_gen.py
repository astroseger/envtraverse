#!/usr/bin/env python3
"""Run generator from a JSON config and print ASCII render.

Usage:
  python scripts/run_gen.py --config configs/demo.json

If you omit --config, an embedded demo config is used.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from labyrinth.config import config_from_dict
from labyrinth.engine import Engine
from labyrinth.generator import generate
from labyrinth.render import render


DEMO = {
    "version": 1,
    "metadata": {"note": "demo"},
    "board": {"n": 7, "m": 7},
    "rng": {"seed": 12345, "algorithm": "py-random-v1"},
    "strategy": {"name": "basic-v1", "params": {}},
    "systems": {
        "walls": {
            "enabled": True,
            "strategy": "degree-v1",
            "target_mean_degree": 1.6,
            "nonriver_engine_enabled": True,
            "river_enabled": True,
            "target_mean_river_walls": 1.0
        },
        "keys": True,
        "treasure": True,
        "exit_key": False,
        "rivers": {
            "enabled": True,
            "strategy": "linear-v1",
            "density": 0.3,
            "mean_len": 1.4,
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

ACTION_ALIASES = {
    # arrows
    "u": "up",
    "d": "down",
    "l": "left",
    "r": "right",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",

    # cardinal directions
    "n": "up",
    "s": "down",
    "w": "left",
    "e": "right",
    "north": "up",
    "south": "down",
    "west": "left",
    "east": "right",

    # look
    "look_up": "look_up",
    "look_down": "look_down",
    "look_left": "look_left",
    "look_right": "look_right",
    "lu": "look_up",
    "ld": "look_down",
    "ll": "look_left",
    "lr": "look_right",

    # bombs
    "bomb_up": "bomb_up",
    "bomb_down": "bomb_down",
    "bomb_left": "bomb_left",
    "bomb_right": "bomb_right",
    "bu": "bomb_up",
    "bd": "bomb_down",
    "bl": "bomb_left",
    "br": "bomb_right",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=str, default=None)
    ap.add_argument("--seed", type=int, default=None, help="override rng.seed from config/demo")
    ap.add_argument("--play", action="store_true", help="interactive play (human text UI)")
    ap.add_argument("--formal", action="store_true", help="print formal (JSON) observation after each move")
    ap.add_argument("--formal-pos", action="store_true", help="include pos in formal output (debug)")
    ap.add_argument("--lang", type=str, default="ru", choices=("ru", "en"), help="text language")
    ap.add_argument("--pits", action="store_true", help="enable pits (override config/demo)")
    ap.add_argument("--pits-allow-on-river", action="store_true", help="allow pits on river cells (override config/demo)")
    ap.add_argument("--grenades", action="store_true", help="enable grenades system (override config/demo)")
    ap.add_argument("--boats", action="store_true", help="enable boats system (override config/demo)")
    args = ap.parse_args()

    d = DEMO
    if args.config:
        d = json.loads(Path(args.config).read_text(encoding="utf-8"))

    if args.seed is not None:
        d = dict(d)
        d["rng"] = dict(d.get("rng", {}))
        d["rng"]["seed"] = int(args.seed)

    # convenient CLI overrides (demo/testing)
    if args.pits or args.pits_allow_on_river or args.grenades or args.boats:
        d = dict(d)
        d["systems"] = dict(d.get("systems", {}))

    if args.pits or args.pits_allow_on_river:
        pits_cfg = dict(d["systems"].get("pits_cfg", {}))
        pits_cfg.setdefault("enabled", False)
        pits_cfg.setdefault("strategy", "cycles-v1")
        pits_cfg.setdefault("cycles", 2)
        pits_cfg.setdefault("cycle_len", 3)
        pits_cfg.setdefault("allow_on_river", False)

        if args.pits:
            pits_cfg["enabled"] = True
        if args.pits_allow_on_river:
            pits_cfg["enabled"] = True
            pits_cfg["allow_on_river"] = True

        d["systems"]["pits_cfg"] = pits_cfg

    if args.grenades:
        gren = dict(d["systems"].get("grenades", {}))
        gren.setdefault("enabled", False)
        gren.setdefault("strategy", "seal-cell-v1")
        gren.setdefault("extra_grenades", 1)
        gren.setdefault("max_walls_to_add", 3)
        gren["enabled"] = True
        d["systems"]["grenades"] = gren

    if args.boats:
        boats = dict(d["systems"].get("boats", {}))
        boats.setdefault("enabled", False)
        boats.setdefault("strategy", "river-exit-v1")
        boats["enabled"] = True
        d["systems"]["boats"] = boats

    # Support both plain config and saved bundle (labyrinth.bundle.v1)
    if isinstance(d, dict) and d.get("format") == "labyrinth.bundle.v1":
        from labyrinth.loader import load_or_generate

        loaded = load_or_generate(d)
        cfg = config_from_dict(loaded.config)
        gen = loaded.generated
    else:
        cfg = config_from_dict(d)
        gen = generate(cfg)

    print(render(gen.board, gen.start))
    print()
    print(f"Solvable: {gen.stats.solvable}; shortest_steps={gen.stats.shortest_steps}; explored={gen.stats.explored_states}")

    if not args.play:
        return

    print("\n--- PLAY MODE ---")
    # show available commands depending on enabled systems
    cmds = ["up/down/left/right (u/d/l/r)", "north/south/west/east (n/s/w/e)"]
    if getattr(gen.config.systems, "flashlight", None) and gen.config.systems.flashlight.enabled:
        cmds.append("look_up/look_down/look_left/look_right (lu/ld/ll/lr)")
    if gen.config.systems.grenades.enabled:
        cmds.append("bomb_up/bomb_down/bomb_left/bomb_right (bu/bd/bl/br)")
    print("Commands: " + ", ".join(cmds) + ", 'state', 'map', 'quit'")

    fl = getattr(gen.config.systems, "flashlight", None)
    fl_enabled = bool(getattr(fl, "enabled", False))
    fl_charges = int(getattr(fl, "charges", 0) or 0)

    eng = Engine(
        gen.board,
        gen.start,
        lang=args.lang,
        require_exit_key_to_leave=gen.goal.require_exit_key_to_leave,
        grenades_enabled=bool(gen.config.systems.grenades.enabled),
        boats_enabled=bool(gen.config.systems.boats.enabled),
        flashlight_enabled=fl_enabled,
        flashlight_charges=fl_charges,
    )

    while True:
        cmd = input("> ").strip().lower()
        if cmd in ("q", "quit", "exit"):
            break
        if cmd == "state":
            snap = eng.snapshot()
            if not args.formal_pos:
                snap.pop("pos", None)
            print(json.dumps(snap, ensure_ascii=False))
            continue

        if cmd == "map":
            print(render(gen.board, eng.state))
            continue

        action = ACTION_ALIASES.get(cmd)
        if action is None:
            print("Не понял команду. Используй up/down/left/right или state/quit")
            continue

        out = eng.step(action)  # type: ignore[arg-type]
        print(out.text)

        if args.formal:
            d = dict(out.obs.__dict__)
            if not args.formal_pos:
                d.pop("pos", None)
            print(json.dumps(d, ensure_ascii=False))

        # auto-finish on exit
        if "exited" in out.obs.events:
            break


if __name__ == "__main__":
    main()
