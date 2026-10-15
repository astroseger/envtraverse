#!/usr/bin/env python3
"""Summarize iterations from agent.log, using server.log only for game names."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import sys
from typing import Any

if __package__:
    from .server_logs import game_server_logs, read_session
else:
    from server_logs import game_server_logs, read_session


@dataclass
class IterationSummary:
    iteration_id: int
    step_index: int
    protocols: list[str] = field(default_factory=list)


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


def nonnegative_integer(value: Any, name: str, location: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{location}: {name} must be a non-negative integer")
    return value


def summarize_log(log_path: Path) -> list[str]:
    iterations: list[IterationSummary] = []
    pending: IterationSummary | None = None
    total_steps = 0
    with log_path.open(encoding="utf-8") as log:
        for line_number, line in enumerate(log, start=1):
            if not line.strip():
                continue
            location = f"{log_path}:{line_number}"
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{location}: invalid JSON: {exc}") from exc
            if not isinstance(event, dict):
                raise ValueError(f"{location}: expected a JSON object")
            if event.get("type") != "external_agent_log":
                continue

            # Only runner-owned, top-level counters are authoritative. Do not
            # parse step numbers from model prose, tool output, or prompts.
            step_index = None
            for key in ("step_index", "n_steps_total"):
                if key in event and event[key] is not None:
                    step_index = nonnegative_integer(event[key], key, location)
                    total_steps = max(total_steps, step_index)
                    break

            message = event.get("message")
            if message == "iteration inspection":
                iteration_id = nonnegative_integer(event.get("iteration_id"), "iteration_id", location)
                if step_index is None:
                    raise ValueError(f"{location}: iteration inspection is missing step_index")
                pending = IterationSummary(iteration_id, step_index)
                iterations.append(pending)
            elif message == "selected protocol" and pending is not None:
                protocol = event.get("protocol")
                if not isinstance(protocol, str) or not protocol.strip():
                    raise ValueError(f"{location}: protocol must be a nonempty string")
                pending.protocols.append(protocol)
            elif message in {
                "agent parameters", "starting agent", "start init_recovery",
                "stopping main loop", "stop condition met", "initialization failed",
                "iteration loop failed", "failed to inspect sessions",
            }:
                # A recovery can select a protocol before its first inspection.
                # That selection must not attach to the previous process's row.
                pending = None

    return [
        f"iteration={item.iteration_id} step={item.step_index}/{total_steps} "
        f"protocol={','.join(item.protocols) if item.protocols else '-'}"
        for item in iterations
    ]


def iter_run_log_paths(runs_dir: Path) -> list[tuple[str, Path]]:
    run_logs = []
    for child in runs_dir.iterdir():
        if not child.is_dir():
            continue
        for log_path in (child / "run" / "agent.log", child / "agent.log"):
            if log_path.is_file():
                run_logs.append((child.name, log_path))
                break
    return run_logs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs_dir", type=Path, help="Run directory containing <alias>/run/agent.log files")
    args = parser.parse_args(argv)
    try:
        runs_dir = args.runs_dir.expanduser().resolve()
        if not runs_dir.is_dir():
            raise ValueError(f"Runs folder does not exist: {runs_dir}")
        names = read_game_names(runs_dir)
        run_logs = iter_run_log_paths(runs_dir)
        if not run_logs:
            raise ValueError(f"No agent.log files found under {runs_dir}")
        run_logs.sort(key=lambda item: (natural_key(names.get(item[0], item[0])), natural_key(item[0])))
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    failed = False
    for index, (name, log_path) in enumerate(run_logs):
        if index:
            print("----------------------")
        print(f"{name} ({names.get(name, 'unknown')})")
        try:
            lines = summarize_log(log_path)
        except (OSError, UnicodeError, ValueError) as exc:
            failed = True
            print(f"ERROR: {exc}")
            continue
        print("\n".join(lines) if lines else "No iteration inspections recorded.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
