#!/usr/bin/env python3
"""Template test for bundle (saved generation) consistency.

Fill BUNDLE_PATH to a saved bundle JSON.
The test will:
- load the saved board
- regenerate from the embedded config
- compare serialized board snapshots

Run:
  python3 projects/labyrinth/tests/test_bundle_roundtrip_template.py
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

THIS = Path(__file__).resolve()
sys.path.insert(0, str(THIS.parents[1] / "src"))

from labyrinth.config import config_from_dict
from labyrinth.generator import generate
from labyrinth.io import board_to_dict, load_bundle


def run(bundle_path: str) -> None:
    bundle = json.loads(Path(bundle_path).read_text(encoding="utf-8"))
    cfg_dict, saved_board, saved_start, saved_goal = load_bundle(bundle)

    regen = generate(config_from_dict(cfg_dict))

    # Compare by serialized representation (order-stable)
    a = board_to_dict(saved_board)
    b = board_to_dict(regen.board)

    from labyrinth.render import render

    if a != b:
        print("SAVED:")
        print(render(saved_board, saved_start))
        print()
        print("REGENERATED:")
        print(render(regen.board, regen.start))
        raise AssertionError("Regenerated board does not match saved board")

    # If OK, print one map (useful for quickly seeing what this bundle is)
    print(render(saved_board, saved_start))
    print("OK: regenerated board matches saved board")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("bundle", type=str, help="Path to labyrinth.bundle.v1 JSON")
    args = ap.parse_args()

    run(args.bundle)
