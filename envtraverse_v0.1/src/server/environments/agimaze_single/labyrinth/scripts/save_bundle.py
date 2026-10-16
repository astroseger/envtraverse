#!/usr/bin/env python3
"""Generate and save a labyrinth bundle (config + generated snapshot) to JSON.

Usage:
  python3 projects/labyrinth/scripts/save_bundle.py --config path/to/config.json --out out.json

If --out is omitted, prints JSON to stdout.

This writes format "labyrinth.bundle.v1".

Optional:
- `--show-map` prints ASCII render of the generated labyrinth.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from labyrinth.config import config_from_dict
from labyrinth.generator import generate
from labyrinth.io import bundle_to_dict


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=str, required=True)
    ap.add_argument("--out", type=str, default=None)
    ap.add_argument("--show-map", action="store_true", help="print ASCII map to stderr")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    cfg_dict = json.loads(cfg_path.read_text(encoding="utf-8"))

    gen = generate(config_from_dict(cfg_dict))

    if args.show_map:
        from labyrinth.render import render

        print(render(gen.board, gen.start), file=sys.stderr)

    bundle = bundle_to_dict(config=cfg_dict, board=gen.board, start=gen.start, goal=gen.goal)
    txt = json.dumps(bundle, ensure_ascii=False, indent=2)

    if args.out:
        Path(args.out).write_text(txt, encoding="utf-8")
    else:
        print(txt)


if __name__ == "__main__":
    main()
