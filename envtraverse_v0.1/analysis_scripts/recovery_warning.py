#!/usr/bin/env python3
"""Warn about recovery attempts recorded in per-game agent_runner.log files."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


RECOVERY_ATTEMPT = re.compile(
    r"(?:^|\s)agent_runner\.py: recovery attempt [1-9]\d*/[1-9]\d*\s*$"
)


def count_recovery_attempts(log_path: Path) -> int:
    # Count the explicit attempt record, not the subsequent --recovery launch
    # or exit messages. Repeated counters still represent separate attempts.
    with log_path.open(encoding="utf-8") as log:
        return sum(RECOVERY_ATTEMPT.search(line) is not None for line in log)


def find_runner_logs(runs_dir: Path) -> list[Path]:
    logs = []
    for child in sorted(runs_dir.iterdir()):
        if not child.is_dir():
            continue
        for log_path in (child / "run" / "agent_runner.log", child / "agent_runner.log"):
            if log_path.is_file():
                logs.append(log_path)
                break
    return logs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "runs_dir", type=Path,
        help="Run directory containing <alias>/run/agent_runner.log files",
    )
    args = parser.parse_args(argv)
    try:
        runs_dir = args.runs_dir.expanduser().resolve()
        if not runs_dir.is_dir():
            raise ValueError(f"Runs folder does not exist: {runs_dir}")
        logs = find_runner_logs(runs_dir)
        if not logs:
            raise ValueError(f"No agent_runner.log files found under {runs_dir}")
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    total_attempts = 0
    affected_games = 0
    failed = False
    for log_path in logs:
        try:
            attempts = count_recovery_attempts(log_path)
        except (OSError, UnicodeError) as exc:
            print(f"Error reading {log_path}: {exc}", file=sys.stderr)
            failed = True
            continue
        if attempts:
            affected_games += 1
            total_attempts += attempts
            print(f"WARNING: {log_path.relative_to(runs_dir)}: {attempts} recovery attempt(s)")

    if failed:
        return 1
    if total_attempts:
        print(f"Found {total_attempts} recovery attempt(s) in {affected_games} of {len(logs)} game(s).")
    else:
        print(f"No recovery attempts found in {len(logs)} agent_runner.log file(s).")
    # Recovery is a warning, not a failure of this analysis script.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
