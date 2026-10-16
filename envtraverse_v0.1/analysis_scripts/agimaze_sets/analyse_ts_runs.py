#!/usr/bin/env python3
"""Refresh run data and score the final target maze of each agimaze_set_v1 set."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis_scripts.agimaze_single import analyse_runs as single


EXTRACTOR_PATHS = single.EXTRACTOR_PATHS
SETS_DIR = ROOT / "src/server/environments/agimaze_set_v1/sets"
ACTIONS_FILENAME = "last_maze_actions.json"
RESULTS_FILENAME = "last_maze_results.json"


def load_set_paths(set_id: str, sets_dir: Path) -> list[str]:
    """Read the ordered maze paths without importing the server or starting it."""
    if set_id in {"", ".", ".."} or "/" in set_id or "\\" in set_id:
        raise ValueError(f"Invalid set ID {set_id!r}: expected a set filename stem without directories")
    sets_dir = sets_dir.resolve()
    path = (sets_dir / f"{set_id}.json").resolve()
    if path.parent != sets_dir:
        raise ValueError(f"Set path escapes sets directory: {set_id!r}")
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: expected a non-empty list of mazes")
    paths = []
    for index, entry in enumerate(entries, start=1):
        location = f"{path}: entry {index}"
        if not isinstance(entry, dict):
            raise ValueError(f"{location} must be an object")
        maze = entry.get("path")
        if not isinstance(maze, str) or not maze.strip():
            raise ValueError(f"{location} requires a non-empty string path")
        if type(entry.get("tutorial", False)) is not bool:
            raise ValueError(f"{location} tutorial must be a boolean")
        paths.append(maze.strip())
    return paths


def extract_last_maze_actions(
    records: list[dict[str, Any]], *, sets_dir: Path = SETS_DIR,
) -> list[dict[str, Any]]:
    targets = []
    for record in records:
        set_id = record["environment_id"]
        mazes = load_set_paths(set_id, sets_dir)
        actions = record["actions"]
        starts = [index for index, action in enumerate(actions) if action["id"].strip() == "start"]
        if len(starts) != len(mazes):
            raise ValueError(
                f"Set {set_id!r}: found {len(starts)} start actions; expected {len(mazes)} "
                "(one per maze). Every maze, including the target, must have been started."
            )
        if starts[0] != 0:
            raise ValueError(f"Set {set_id!r}: actions appear before the first start command")
        targets.append({
            **record,
            "environment_id": mazes[-1],
            "actions": actions[starts[-1] + 1:],
            "set_id": set_id,
            "labyrinth_index": len(mazes) - 1,
        })
    return targets


def replay_last_maze_results(
    targets: list[dict[str, Any]], *, replayer: single.Replayer | None = None,
) -> list[dict[str, Any]]:
    results = single.replay_results(targets, replayer=replayer)
    for result, target in zip(results, targets):
        result["set_id"] = target["set_id"]
        result["labyrinth_index"] = target["labyrinth_index"]
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_folder", type=Path, help="Run directory containing <alias>/server/server.log and per-game agent logs")
    args = parser.parse_args(argv)
    try:
        run_folder = args.run_folder.expanduser().resolve()
        for extractor_path in EXTRACTOR_PATHS:
            subprocess.run([sys.executable, str(extractor_path), str(run_folder)], check=True)
        records = single.read_raw_actions(run_folder / "raw_actions.json")
        targets = extract_last_maze_actions(records, sets_dir=SETS_DIR)
        results = replay_last_maze_results(targets)
        # Validate and replay the entire run before replacing either derived file.
        single.write_results(run_folder / ACTIONS_FILENAME, targets)
        single.write_results(run_folder / RESULTS_FILENAME, results)
    except subprocess.CalledProcessError as exc:
        print(f"Error: {extractor_path.name} failed; target-maze analysis was not updated.", file=sys.stderr)
        return exc.returncode if exc.returncode > 0 else 1
    except (OSError, UnicodeError, ImportError, RuntimeError, TypeError, ValueError) as exc:
        print(f"Analysis failed: {exc}", file=sys.stderr)
        return 1

    solved = sum(result["solved"] for result in results)
    actions = sum(result["steps"] for result in results)
    print(f"Wrote {run_folder / ACTIONS_FILENAME} ({len(targets)} target mazes)")
    print(f"Wrote {run_folder / RESULTS_FILENAME} ({solved}/{len(results)} solved, {actions} target-maze actions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
