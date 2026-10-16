#!/usr/bin/env python3
"""Print attempts and accepted actions per level from <run>/<alias>/observations/."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys
from typing import Any


STEP_METADATA_RE = re.compile(r"^step_(\d+)_metadata\.txt$")
if __package__:
    from ..server_logs import game_server_logs, read_session
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from server_logs import game_server_logs, read_session


def natural_key(name: str) -> list[Any]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


def read_game_names(run_folder: Path) -> dict[str, str]:
    """Use optional per-game server logs to label anonymous game folders."""
    names = {}
    for log_path in game_server_logs(run_folder, required=False):
        session = read_session(log_path)
        if session is not None:
            names[log_path.parent.parent.name] = session[1]
    return names


def read_metadata(path: Path) -> dict[str, Any]:
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(metadata, dict):
        raise ValueError(f"{path}: metadata must be an object")
    return metadata


def metadata_int(metadata: dict[str, Any], key: str, path: Path) -> int:
    value = metadata.get(key)
    if type(value) is not int or value < 0:
        raise ValueError(f"{path}: {key} must be a non-negative integer")
    return value


def metadata_state(metadata: dict[str, Any], path: Path) -> str:
    state = metadata.get("state")
    if not isinstance(state, str):
        raise ValueError(f"{path}: state must be a string")
    return state


def analyze_observations(observations_dir: Path) -> tuple[Counter[str], Counter[str], bool] | None:
    """Match the old counter's rules; return None when no step metadata exists."""
    files = []
    for path in observations_dir.iterdir():
        match = STEP_METADATA_RE.fullmatch(path.name)
        if match and path.is_file():
            files.append((int(match.group(1)), path))
    files.sort(key=lambda item: item[0])
    if not files:
        return None
    for expected_step, (step, path) in enumerate(files):
        if step != expected_step:
            raise ValueError(
                f"metadata steps in {observations_dir} must be consecutive from 0; "
                f"expected {expected_step}, found {step} ({path.name})"
            )

    first_path = files[0][1]
    first_metadata = read_metadata(first_path)
    win_levels = metadata_int(first_metadata, "win_levels", first_path)
    if win_levels == 0:
        raise ValueError(f"{first_path}: win_levels must be positive")
    previous_completed = metadata_int(first_metadata, "levels_completed", first_path)
    if previous_completed > win_levels:
        raise ValueError(f"{first_path}: levels_completed exceeds win_levels")
    previous_state = metadata_state(first_metadata, first_path)
    resets: Counter[str] = Counter()
    steps: Counter[str] = Counter()

    # Step 0 is the initial observation, not an accepted action or a new reset.
    for _, path in files[1:]:
        metadata = read_metadata(path)
        if metadata_int(metadata, "win_levels", path) != win_levels:
            raise ValueError(f"{path}: win_levels changed from {win_levels}")
        current_completed = metadata_int(metadata, "levels_completed", path)
        if current_completed > win_levels:
            raise ValueError(f"{path}: levels_completed exceeds win_levels")
        if current_completed not in {previous_completed, previous_completed + 1}:
            raise ValueError(f"{path}: levels_completed changed from {previous_completed} to {current_completed}")
        if previous_state == "WIN" or previous_completed == win_levels:
            raise ValueError(f"{path}: action recorded after all levels were completed")
        action = metadata.get("action_input")
        if not isinstance(action, dict) or not isinstance(action.get("id"), str):
            raise ValueError(f"{path}: action_input must be an object with a string id")

        # Attribute the action (including a level-completing move or RESET) to
        # the level in the preceding observation, not the resulting one.
        level = f"{previous_completed + 1:02d}"
        steps[level] += 1
        if action["id"] == "RESET":
            resets[level] += 1
        previous_completed = current_completed
        previous_state = metadata_state(metadata, path)

    completed = previous_state == "WIN"
    last_path = files[-1][1]
    if completed and previous_completed != win_levels:
        raise ValueError(f"{last_path}: WIN state has {previous_completed} of {win_levels} levels completed")
    if not completed and previous_completed == win_levels:
        raise ValueError(f"{last_path}: all {win_levels} levels are completed but state is not WIN")

    reached_levels = previous_completed if completed else previous_completed + 1
    attempts: Counter[str] = Counter()
    for number in range(1, reached_levels + 1):
        level = f"{number:02d}"
        attempts[level] = resets[level] + 1
        steps[level] += 0
    return attempts, steps, completed


def format_counts(attempts: Counter[str], steps: Counter[str], completed: bool) -> str:
    parts = [f"{level}/{attempts[level]}/{steps[level]}" for level in sorted(attempts, key=int)]
    if completed:
        parts.append("WIN")
    return ", ".join(parts)


def results_for_run(run_folder: Path) -> list[str]:
    names = read_game_names(run_folder)
    game_dirs = [path for path in run_folder.iterdir() if (path / "observations").is_dir()]
    game_dirs.sort(key=lambda path: (natural_key(names.get(path.name, path.name)), natural_key(path.name)))
    lines = []
    for game_dir in game_dirs:
        game = names.get(game_dir.name)
        label = f"{game_dir.name} ({game})" if game else game_dir.name
        result = analyze_observations(game_dir / "observations")
        details = "no observations" if result is None else format_counts(*result)
        lines.append(f"{label}: {details}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_folder", type=Path, help="Run directory containing <alias>/observations folders")
    args = parser.parse_args(argv)
    try:
        run_folder = args.run_folder.expanduser().resolve()
        if not run_folder.is_dir():
            raise ValueError(f"not a directory: {run_folder}")
        lines = results_for_run(run_folder)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    # Validate the complete input before printing, so errors cannot look like a
    # successful report that silently omitted later games.
    print("\n".join(lines) if lines else f"No observations directories found under {run_folder}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
