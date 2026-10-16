#!/usr/bin/env python3
"""Refresh curriculum analysis and report target-maze scores plus whole-set cost."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis_scripts.agimaze_single import print_results_md_v1 as single
from analysis_scripts.agimaze_sets.analyse_ts_runs import RESULTS_FILENAME


ANALYSIS_PATH = Path(__file__).resolve().with_name("analyse_ts_runs.py")


def load_results(path: Path) -> tuple[list[single.LabyrinthResult], set[str]]:
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise ValueError(f"Expected a list in results file {path}")
    results = []
    set_ids = []
    seen_sets: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        location = f"{path}: entry {index}"
        result = single.parse_result(entry, location)
        set_id = entry.get("set_id")
        if not isinstance(set_id, str) or not set_id.strip():
            raise ValueError(f"{location} requires a non-empty string set_id")
        set_id = set_id.strip()
        if set_id in seen_sets:
            raise ValueError(f"{location}: duplicate set_id {set_id!r}")
        seen_sets.add(set_id)
        set_ids.append(set_id)
        results.append(result)

    # Different curricula may target the same maze; do not merge their results.
    counts = Counter(result.name for result in results)
    results = [
        replace(result, name=f"{result.name} ({set_id})") if counts[result.name] > 1 else result
        for result, set_id in zip(results, set_ids)
    ]
    return results, seen_sets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_folder", type=Path, help="Run directory containing <alias>/server/server.log and per-game agent logs")
    args = parser.parse_args(argv)
    try:
        run_folder = args.run_folder.expanduser().resolve()
        # Always refresh, keeping extractor/replay progress out of Markdown stdout.
        subprocess.run([sys.executable, str(ANALYSIS_PATH), str(run_folder)], check=True, stdout=sys.stderr)
        results, set_ids = load_results(run_folder / RESULTS_FILENAME)
        cost = single.load_total_cost(run_folder / single.COST_TOKENS_FILENAME, set_ids)
        output = (
            "Scores and action counts cover only the final maze of each set.\n\n"
            + single.render_markdown(single.order_results(results))
            + f"\ntotal cost (whole sets, including curriculum): ${cost:,.2f}"
            + "\n\n" + single.render_numeric_summary(results, cost)
        )
    except subprocess.CalledProcessError as exc:
        print("Error: analysis failed; no report was generated.", file=sys.stderr)
        return exc.returncode if exc.returncode > 0 else 1
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
