#!/usr/bin/env python3
import argparse
import json
import re
from collections import Counter
from pathlib import Path


STEP_METADATA_RE = re.compile(r"^step_(\d+)_metadata\.txt$")


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc


def _read_metadata_sequence(observations_dir):
    metadata_files = []

    for path in observations_dir.iterdir():
        if not path.is_file():
            continue
        match = STEP_METADATA_RE.match(path.name)
        if match:
            metadata_files.append((int(match.group(1)), path))

    metadata_files.sort(key=lambda item: item[0])
    if not metadata_files:
        raise ValueError(f"no step metadata files found in {observations_dir}")

    actual_steps = [step for step, _ in metadata_files]
    expected_steps = list(range(len(metadata_files)))
    if actual_steps != expected_steps:
        raise ValueError(
            f"metadata steps in {observations_dir} must be consecutive from 0; "
            f"found {actual_steps}"
        )

    return [(step, path, _read_json(path)) for step, path in metadata_files]


def _metadata_int(metadata, key, path):
    value = metadata.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{path}: {key} must be a non-negative integer")
    return value


def analyze_observations(observations_dir):
    observations_dir = Path(observations_dir)
    sequence = _read_metadata_sequence(observations_dir)
    reset_counts = Counter()
    steps = Counter()

    first_step, first_path, first_metadata = sequence[0]
    assert first_step == 0
    win_levels = _metadata_int(first_metadata, "win_levels", first_path)
    if win_levels == 0:
        raise ValueError(f"{first_path}: win_levels must be positive")

    previous_completed = _metadata_int(first_metadata, "levels_completed", first_path)
    if previous_completed > win_levels:
        raise ValueError(f"{first_path}: levels_completed exceeds win_levels")

    previous_state = first_metadata.get("state")
    if not isinstance(previous_state, str):
        raise ValueError(f"{first_path}: state must be a string")

    for step, path, metadata in sequence[1:]:
        current_win_levels = _metadata_int(metadata, "win_levels", path)
        if current_win_levels != win_levels:
            raise ValueError(
                f"{path}: win_levels changed from {win_levels} to {current_win_levels}"
            )

        current_completed = _metadata_int(metadata, "levels_completed", path)
        if current_completed > win_levels:
            raise ValueError(f"{path}: levels_completed exceeds win_levels")
        if current_completed not in {previous_completed, previous_completed + 1}:
            raise ValueError(
                f"{path}: levels_completed changed from {previous_completed} "
                f"to {current_completed}"
            )
        if previous_state == "WIN":
            raise ValueError(f"{path}: action recorded after the game was won")

        action_input = metadata.get("action_input")
        if not isinstance(action_input, dict):
            raise ValueError(f"{path}: action_input must be an object")
        action_id = action_input.get("id")
        if not isinstance(action_id, str):
            raise ValueError(f"{path}: action_input.id must be a string")

        # This action was taken before the resulting metadata was produced, so it
        # belongs to the level reported by the preceding metadata. This includes
        # the action that completes a level.
        level = previous_completed + 1
        if level > win_levels:
            raise ValueError(f"{path}: action recorded after all levels were completed")
        level_key = str(level).zfill(2)
        steps[level_key] += 1
        if action_id == "RESET":
            reset_counts[level_key] += 1

        current_state = metadata.get("state")
        if not isinstance(current_state, str):
            raise ValueError(f"{path}: state must be a string")
        previous_completed = current_completed
        previous_state = current_state

    completed = previous_state == "WIN"
    if completed and previous_completed != win_levels:
        last_path = sequence[-1][1]
        raise ValueError(
            f"{last_path}: WIN state has {previous_completed} of {win_levels} levels completed"
        )
    if not completed and previous_completed == win_levels:
        last_path = sequence[-1][1]
        raise ValueError(
            f"{last_path}: all {win_levels} levels are completed but state is not WIN"
        )

    reached_levels = previous_completed if completed else previous_completed + 1
    attempts = Counter()
    for level in range(1, reached_levels + 1):
        level_key = str(level).zfill(2)
        attempts[level_key] = reset_counts[level_key] + 1
        steps[level_key] += 0

    return attempts, steps, completed


def count_level_attempts(observations_dir):
    attempts, _, _ = analyze_observations(observations_dir)
    return attempts


def count_level_steps(observations_dir):
    _, steps, _ = analyze_observations(observations_dir)
    return steps


def game_completed(observations_dir):
    _, _, completed = analyze_observations(observations_dir)
    return completed


def game_name_from_folder_name(folder_name):
    if len(folder_name) >= 4:
        return folder_name[:4]
    return None


def game_name_from_observations_folder(observations_dir):
    try:
        folder_name = observations_dir.parents[3].name
    except IndexError:
        return None
    return game_name_from_folder_name(folder_name)


def format_label(name, game_name):
    if game_name:
        return f"{name} ({game_name})"
    return name


def format_counts(counts, completed=False, steps=None):
    parts = [
        f"{level}/{counts[level]}/{steps[level]}"
        if steps is not None
        else f"{level}/{counts[level]}"
        for level in sorted(counts, key=int)
    ]
    if completed:
        parts.append("WIN")
    return ", ".join(parts)


def main():
    parser = argparse.ArgumentParser(
        description="Count attempts and accepted actions per level from observation metadata."
    )
    parser.add_argument("observations_dir", help="Path to the client observations directory")
    args = parser.parse_args()

    observations_dir = Path(args.observations_dir)
    if not observations_dir.is_dir():
        parser.error(f"not a directory: {observations_dir}")

    try:
        attempts, steps, completed = analyze_observations(observations_dir)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    label = format_label(
        observations_dir.name,
        game_name_from_observations_folder(observations_dir),
    )
    print(f"{label}: {format_counts(attempts, completed, steps)}")


if __name__ == "__main__":
    main()
