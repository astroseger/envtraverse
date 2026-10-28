#!/usr/bin/env python3
"""Extract actions and replay ARC3 games into <environment_id>_scorecard.json."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any


EXTRACTOR_PATH = Path(__file__).resolve().parents[1] / "extract_raw_actions.py"


def read_raw_actions(path: Path) -> list[dict[str, Any]]:
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError(f"{path}: expected a list of game records")
    seen_ids: set[str] = set()
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValueError(f"{path}: record {index} must be an object")
        environment_id = record.get("environment_id")
        # IDs are also output filenames. Do not normalize or resolve versions.
        if not isinstance(environment_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", environment_id):
            raise ValueError(f"{path}: record {index} has an invalid ARC3 environment_id")
        if environment_id in seen_ids:
            raise ValueError(f"{path}: duplicate environment_id {environment_id!r}")
        seen_ids.add(environment_id)
    return records


def action_coordinates(action: dict[str, Any]) -> dict[str, int]:
    """Match the server's ACTION6 coordinate normalization."""
    data = action.get("data")
    if not isinstance(data, dict):
        raise ValueError("ACTION6 requires a data object with x and y")
    coordinates = {}
    for name in ("x", "y"):
        value = data.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError(f"ACTION6 {name} must be an integer")
        try:
            coordinate = int(value)
        except ValueError:
            raise ValueError(f"ACTION6 {name} must be an integer") from None
        if not 0 <= coordinate <= 63:
            raise ValueError(f"ACTION6 {name} must be between 0 and 63")
        coordinates[name] = coordinate
    return coordinates


def replay_actions(environment_id: str, actions: Any) -> dict[str, Any]:
    if not isinstance(actions, list):
        raise ValueError("actions must be a list")
    try:
        from arc_agi import Arcade, OperationMode
        from arcengine import GameAction
    except ImportError as exc:
        raise RuntimeError(
            "ARC replay requires arc-agi and arcengine in the Python interpreter running this script."
        ) from exc

    # The SDK also reads OPERATION_MODE from .env during import. Do not let host
    # configuration turn an analysis replay into remote/competition gameplay.
    operation_mode = os.environ.get("OPERATION_MODE", "").strip().lower()
    if operation_mode not in {"", "normal", "offline"}:
        raise RuntimeError("Replay requires local mode; unset OPERATION_MODE or set it to NORMAL/OFFLINE.")
    arc = Arcade(operation_mode=OperationMode.NORMAL, logger=logging.getLogger("arc3-replay"))
    try:
        environment = arc.make(environment_id, seed=0, render_mode=None, save_recording=False)
        if environment is None or environment.observation_space is None:
            raise RuntimeError(f"Could not initialize environment {environment_id!r}")

        for index, action in enumerate(actions, start=1):
            try:
                if not isinstance(action, dict):
                    raise ValueError("action must be an object")
                action_id = action.get("id")
                if not isinstance(action_id, str) or not action_id.strip():
                    raise ValueError("action requires a string id")
                if action_id.strip() == "highlight_pixel":
                    continue
                try:
                    game_action = GameAction[action_id.strip()]
                except KeyError:
                    raise ValueError(f"Unknown GameAction {action_id!r}") from None

                if game_action == GameAction.ACTION6:
                    data = action_coordinates(action)
                else:
                    if any(name in action for name in ("data", "x", "y")):
                        raise ValueError("coordinates are only valid with ACTION6")
                    data = None

                if game_action == GameAction.RESET:
                    frame = environment.reset()
                else:
                    frame = environment.step(game_action, data=data)
                if frame is None:
                    raise RuntimeError("environment returned no frame")
            except Exception as exc:
                raise RuntimeError(f"action {index} {action!r}: {exc}") from exc
    finally:
        scorecard = arc.close_scorecard()

    if scorecard is None:
        raise RuntimeError("Replay returned no scorecard")
    return scorecard.model_dump(mode="json")


def write_scorecard(path: Path, scorecard: dict[str, Any]) -> None:
    # Finish replay and serialization before replacing any previous result.
    output = json.dumps(scorecard, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False,
    ) as handle:
        temporary_path = Path(handle.name)
        try:
            handle.write(output)
            handle.close()
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_folder", type=Path, help="Run directory containing <alias>/server/server.log")
    args = parser.parse_args(argv)

    try:
        run_folder = args.run_folder.expanduser().resolve()
        subprocess.run([sys.executable, str(EXTRACTOR_PATH), str(run_folder)], check=True)
        records = read_raw_actions(run_folder / "raw_actions.json")
    except subprocess.CalledProcessError as exc:
        print("Error: action extraction failed; no scorecards were generated.", file=sys.stderr)
        return exc.returncode if exc.returncode > 0 else 1
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    failed = 0
    for record in records:
        environment_id = record["environment_id"]
        output_path = run_folder / f"{environment_id}_scorecard.json"
        try:
            scorecard = replay_actions(environment_id, record.get("actions"))
            write_scorecard(output_path, scorecard)
        except Exception as exc:
            failed += 1
            print(f"{environment_id}: replay failed: {exc}", file=sys.stderr)
            continue
        print(f"{environment_id}: wrote {output_path}", flush=True)

    print(f"Generated {len(records) - failed} scorecards; {failed} failed.", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
