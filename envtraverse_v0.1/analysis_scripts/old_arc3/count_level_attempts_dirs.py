#!/usr/bin/env python3
import argparse
import re
from pathlib import Path

from count_level_attempts import (
    analyze_observations,
    format_counts,
    format_label,
    game_name_from_folder_name,
)


def natural_key(path):
    parts = re.split(r"(\d+)", path.name)
    return [int(part) if part.isdigit() else part for part in parts]


def iter_observations_dirs(home_root):
    for home_dir in sorted(home_root.iterdir(), key=natural_key):
        observations_dir = home_dir / "run" / "agent_run" / "client" / "observations"
        try:
            is_observations_dir = observations_dir.is_dir()
        except OSError:
            continue

        if is_observations_dir:
            yield home_dir.name, observations_dir


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Scan <root>/<name>/run/agent_run/client/observations directories "
            "and print attempts and accepted actions per level."
        )
    )
    parser.add_argument(
        "home_root",
        nargs="?",
        default="/home",
        help="Root directory containing run homes (default: /home)",
    )
    args = parser.parse_args()

    home_root = Path(args.home_root)
    if not home_root.is_dir():
        parser.error(f"not a directory: {home_root}")

    found = False
    for name, observations_dir in iter_observations_dirs(home_root):
        found = True
        try:
            counts, steps, completed = analyze_observations(observations_dir)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        label = format_label(name, game_name_from_folder_name(name))
        print(f"{label}: {format_counts(counts, completed, steps)}")

    if not found:
        print(f"No observations directories found under {home_root}")


if __name__ == "__main__":
    main()
