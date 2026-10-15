#!/usr/bin/env python3
"""Refresh token counts, then report Terminal-Bench scores, cost tokens, and recovery.

Pass the project directory containing run/ and run_tb/. Each result.json counts
once in the mean, including null/non-numeric rewards (scored as zero). A failed
recovery means the last nonempty runner-log line is not the normal stop message.
Cybersecurity failures count only failed recoveries whose final two agent-log
messages match the turn.failed / initialization failed pattern.
The last line groups runs by termination_reason and reports how many have a
reward other than 1, out of the total for that reason.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
import math
from pathlib import Path
import subprocess
import sys
from typing import Any

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from analysis_scripts.arc3.print_results_md_cost_v2 import cost_tokens, read_json
from analysis_scripts.recovery_warning import RECOVERY_ATTEMPT


EXTRACTOR_PATH = Path(__file__).resolve().parents[1] / "extract_raw_cost_tokens.py"
NORMAL_STOP = "agent_runner.py: stop condition met; stopping supervisor"


@dataclass(frozen=True)
class GameResult:
    game_id: str
    score: float
    recovery_attempts: int
    failed_recovery: bool
    failed_because_of_cyber_security: bool
    encountered_cyber_security_error: bool
    termination_reason: str


def reward_score(rewards: Any) -> float:
    value = rewards.get("reward") if isinstance(rewards, dict) else rewards
    if type(value) not in (int, float) or not math.isfinite(value):
        return 0.0
    return float(value)


def read_runner_log(path: Path) -> tuple[int, bool]:
    attempts = 0
    last = ""
    with path.open(encoding="utf-8") as log:
        for line in log:
            attempts += RECOVERY_ATTEMPT.search(line) is not None
            if line.strip():
                last = line.strip()
    return attempts, not last.endswith(NORMAL_STOP)


def is_cyber_security_error(event: Any) -> bool:
    if not isinstance(event, dict) or event.get("type") != "turn.failed":
        return False
    error = json.dumps(event.get("error"), ensure_ascii=False).casefold()
    return "security" in error and "cyber" in error


def read_agent_log(path: Path) -> tuple[bool, bool]:
    """Return (final cybersecurity failure pattern, ever encountered error).

    Keep only the last two nonempty messages. An unparseable line breaks the
    final-message pattern; it must not make an earlier error look like the end.
    """
    previous = last = None
    encountered = False
    with path.open(encoding="utf-8") as log:
        for line in log:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                event = None
            previous, last = last, event
            encountered |= is_cyber_security_error(event)
    initialization_failed = isinstance(last, dict) and all(
        last.get(key) == value for key, value in {
            "type": "external_agent_log", "level": "ERROR",
            "logger": "simple_agent", "message": "initialization failed",
        }.items()
    )
    return initialization_failed and is_cyber_security_error(previous), encountered


def load_results(project_dir: Path) -> list[GameResult]:
    paths = sorted((project_dir / "run_tb" / "results").glob("*/result.json"))
    if not paths:
        raise ValueError(f"No results found: {project_dir}/run_tb/results/*/result.json")
    results = []
    seen = set()
    for path in paths:
        raw = read_json(path)
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: expected a result object")
        game_id = raw.get("game_id")
        if not isinstance(game_id, str) or not game_id.strip():
            raise ValueError(f"{path}: expected a nonempty game_id")
        if game_id in seen:
            raise ValueError(f"{path}: duplicate game_id {game_id!r}")
        seen.add(game_id)
        termination_reason = raw.get("termination_reason")
        if termination_reason is None:
            termination_reason = "unknown"
        if not isinstance(termination_reason, str) or not termination_reason.strip():
            raise ValueError(f"{path}: expected a nonempty termination_reason")
        # Derive paths from this archive, not absolute paths recorded on its host.
        logs_dir = project_dir / "run" / path.parent.name / "run"
        attempts, failed = read_runner_log(logs_dir / "agent_runner.log")
        final_cyber_error, encountered = read_agent_log(logs_dir / "agent.log")
        results.append(GameResult(
            game_id=game_id, score=reward_score(raw.get("rewards")),
            recovery_attempts=attempts, failed_recovery=failed,
            failed_because_of_cyber_security=failed and final_cyber_error,
            encountered_cyber_security_error=encountered,
            termination_reason=termination_reason,
        ))
    return sorted(results, key=lambda result: result.game_id)


def load_total_cost_tokens(path: Path, game_ids: set[str]) -> float:
    records = read_json(path)
    if not isinstance(records, list):
        raise ValueError(f"{path}: expected a list of token records")
    tokens = {}
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValueError(f"{path}: record {index} must be an object")
        game_id = record.get("environment_id")
        if not isinstance(game_id, str) or not game_id.strip():
            raise ValueError(f"{path}: record {index} has an invalid environment_id")
        if game_id in tokens:
            raise ValueError(f"{path}: duplicate environment_id {game_id!r}")
        tokens[game_id] = record
    if missing := game_ids - tokens.keys():
        raise ValueError(f"{path}: missing token records for {', '.join(sorted(missing))}")
    if extra := tokens.keys() - game_ids:
        raise ValueError(f"{path}: token records without results for {', '.join(sorted(extra))}")
    token_totals = []
    for game_id, record in tokens.items():
        try:
            token_totals.append(cost_tokens(record))
        except ValueError as exc:
            raise ValueError(f"{path}: {game_id}: {exc}") from exc
    return math.fsum(token_totals)


def render_markdown(results: list[GameResult], total_cost_tokens: float) -> str:
    headers = ("game_id", "score", "N_recovery")
    data = []
    for result in results:
        game_id = result.game_id.replace("|", "\\|").replace("\n", " ").replace("\r", " ")
        data.append((game_id, f"{result.score:g}", str(result.recovery_attempts)))
    widths = tuple(
        max([len(header), *(len(row[column]) for row in data)])
        for column, header in enumerate(headers)
    )

    def format_row(row: tuple[str, str, str]) -> str:
        return (f"| {row[0]:<{widths[0]}} | {row[1]:>{widths[1]}} "
                f"| {row[2]:>{widths[2]}} |")

    rows = [format_row(headers), format_row((
        "-" * widths[0], "-" * (widths[1] - 1) + ":", "-" * (widths[2] - 1) + ":",
    ))]
    rows.extend(format_row(row) for row in data)
    total_score = math.fsum(result.score for result in results)
    failed = sum(result.failed_recovery for result in results)
    cyber_failed = sum(result.failed_because_of_cyber_security for result in results)
    encountered = sum(result.encountered_cyber_security_error for result in results)
    termination_totals = Counter(result.termination_reason for result in results)
    termination_failures = Counter(result.termination_reason for result in results if result.score != 1)
    rows.extend([
        "",
        f"score: {total_score / len(results):.6f} ({total_score:g}/{len(results)})",
        f"cost tokens (M): {total_cost_tokens / 1_000_000:,.2f}",
        f"n_fail_recovery: {failed} ({cyber_failed} because of cybersecurity errors)",
        f"n_encounter_cyber_security_error: {encountered}",
        ",  ".join(
            f"{reason}: {termination_failures[reason]}/{termination_totals[reason]}"
            for reason in sorted(termination_totals)
        ),
    ])
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_dir", type=Path, help="Project directory containing run/ and run_tb/")
    args = parser.parse_args(argv)
    try:
        project_dir = args.project_dir.expanduser().resolve()
        run_dir = project_dir / "run"
        if not run_dir.is_dir():
            raise ValueError(f"Run directory does not exist: {run_dir}")
        # Refresh on every invocation and keep stdout exclusively for the report.
        subprocess.run([sys.executable, str(EXTRACTOR_PATH), str(run_dir)],
                       check=True, stdout=sys.stderr)
        results = load_results(project_dir)
        total_cost_tokens = load_total_cost_tokens(run_dir / "raw_cost_tokens.json", {r.game_id for r in results})
        output = render_markdown(results, total_cost_tokens)
    except subprocess.CalledProcessError as exc:
        print(f"Error: extract_raw_cost_tokens.py failed with exit code {exc.returncode}; no report generated.",
              file=sys.stderr)
        return exc.returncode if exc.returncode > 0 else 1
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
