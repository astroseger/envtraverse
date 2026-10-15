#!/usr/bin/env python3
"""Refresh agimaze analysis, then report non-training results and cost tokens in millions."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Any


ANALYSIS_PATH = Path(__file__).resolve().with_name("analyse_runs.py")
RESULTS_FILENAME = "results.json"
COST_TOKENS_FILENAME = "raw_cost_tokens.json"

GROUP_ORDER = {"TUTORIAL": 0, "CLASSIC": 1, "EXTENDED": 2}
DIFFICULTY_ORDER = {"EASY": 0, "MEDIUM": 1, "HARD": 2}


@dataclass(frozen=True)
class LabyrinthResult:
    name: str
    steps: int
    max_steps: int
    solved: bool


def _required_int(entry: dict[str, Any], key: str, location: str, minimum: int) -> int:
    value = entry.get(key)
    if type(value) is not int or value < minimum:
        comparison = "positive" if minimum == 1 else "non-negative"
        raise ValueError(f"{location} requires a {comparison} integer {key!r}")
    return value


def parse_result(entry: Any, location: str) -> LabyrinthResult:
    """Validate one maze result, including its replay-confirmed solved status."""
    if not isinstance(entry, dict):
        raise ValueError(f"{location} must be an object")
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{location} requires a non-empty string 'name'")
    solved = entry.get("solved")
    if type(solved) is not bool:
        raise ValueError(f"{location} requires a boolean 'solved'")
    return LabyrinthResult(
        name=name.strip(),
        steps=_required_int(entry, "steps", location, 0),
        max_steps=_required_int(entry, "max_steps", location, 1),
        solved=solved,
    )


def load_results(path: Path) -> list[LabyrinthResult]:
    """Load non-training labyrinths for the table and all report totals."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected a list in results file {path}")
    results = []
    seen_names: set[str] = set()
    for index, entry in enumerate(payload, start=1):
        location = f"{path}: entry {index}"
        result = parse_result(entry, location)
        if result.name.startswith("TRAINING"):
            continue
        if result.name in seen_names:
            raise ValueError(f"{location}: duplicate labyrinth name {result.name!r}")
        seen_names.add(result.name)
        results.append(result)
    return results


def cost_tokens(tokens: dict[str, Any]) -> float:
    input_tokens = _required_int(tokens, "input_tokens", "Token record", 0)
    cached_tokens = _required_int(tokens, "cached_input_tokens", "Token record", 0)
    output_tokens = _required_int(tokens, "output_tokens", "Token record", 0)
    if cached_tokens > input_tokens:
        raise ValueError("cached_input_tokens cannot exceed input_tokens")
    # Match the ARC3 report: normalize to one output token. Cached input is
    # already included in input_tokens.
    return cached_tokens / 60 + (input_tokens - cached_tokens) / 6 + output_tokens


def load_total_cost_tokens(
    path: Path, environment_ids: set[str], *, excluded_environment_ids: set[str] | None = None,
) -> float:
    """Sum cost tokens, requiring one record per included environment ID."""
    excluded_environment_ids = excluded_environment_ids or set()
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Missing {path}; run analysis_scripts/extract_raw_cost_tokens.py first") from exc
    if not isinstance(records, list):
        raise ValueError(f"{path}: expected a list of token records")
    seen_names: set[str] = set()
    total = 0.0
    for index, record in enumerate(records, start=1):
        location = f"{path}: record {index}"
        if not isinstance(record, dict):
            raise ValueError(f"{location} must be an object")
        name = record.get("environment_id")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{location} requires a non-empty string environment_id")
        name = name.strip()
        if name.startswith("TRAINING") or name in excluded_environment_ids:
            continue
        if name in seen_names:
            raise ValueError(f"{path}: duplicate environment_id {name!r}")
        seen_names.add(name)
        try:
            total += cost_tokens(record)
        except ValueError as exc:
            raise ValueError(f"{path}: {name}: {exc}") from exc

    if missing := environment_ids - seen_names:
        raise ValueError(f"{path}: missing token records for {', '.join(sorted(missing))}; run extract_raw_cost_tokens.py first")
    if extra := seen_names - environment_ids:
        raise ValueError(f"{path}: token records without results for {', '.join(sorted(extra))}")
    return total


def order_results(results: list[LabyrinthResult]) -> list[LabyrinthResult]:
    def key(result: LabyrinthResult) -> tuple[Any, ...]:
        parts = PurePosixPath(result.name).parts
        group = parts[0] if parts else ""
        difficulty = DIFFICULTY_ORDER.get(parts[1], 3) if group == "CLASSIC" and len(parts) > 1 else 0
        natural_name = tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", result.name))
        return GROUP_ORDER.get(group, 3), difficulty, natural_name, result.name

    # This reproduces the old pack ordering without reading order.json. New
    # stages and multiple maze filenames in a category also sort naturally.
    return sorted(results, key=key)


def markdown_cell(value: str) -> str:
    return (value.replace("\\", "\\\\").replace("|", "\\|")
            .replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>"))


def solved_tiers(result: LabyrinthResult) -> tuple[int, int, int]:
    return (
        int(result.solved),
        int(result.solved and result.steps <= 2 * result.max_steps),
        int(result.solved and result.steps <= result.max_steps),
    )


def penalized_step_ratio(result: LabyrinthResult) -> float:
    return result.steps / result.max_steps if result.solved else 5.0


def render_markdown(results: list[LabyrinthResult]) -> str:
    headers = ("name", "steps/max_steps", "x5/x2/x1 solved", "penalized step ratio")
    rows = [
        (markdown_cell(result.name), f"{result.steps}/{result.max_steps}",
         "/".join(str(value) for value in solved_tiers(result)), f"{penalized_step_ratio(result):.2f}")
        for result in results
    ]
    widths = tuple(
        max([len(header), *(len(row[column]) for row in rows)])
        for column, header in enumerate(headers)
    )
    separators = (
        "-" * widths[0],
        "-" * (widths[1] - 1) + ":",
        "-" * (widths[2] - 1) + ":",
        "-" * (widths[3] - 1) + ":",
    )
    lines = [
        "Penalized step ratio: `steps/max_steps` for solved labyrinths and `5` for unsolved labyrinths; "
        "lower is better.",
        "",
        f"| {headers[0]:<{widths[0]}} | {headers[1]:<{widths[1]}} "
        f"| {headers[2]:<{widths[2]}} | {headers[3]:<{widths[3]}} |",
        f"| {separators[0]} | {separators[1]} | {separators[2]} | {separators[3]} |",
    ]
    for name, step_ratio, tier_status, penalized_ratio in rows:
        lines.append(
            f"| {name:<{widths[0]}} | {step_ratio:>{widths[1]}} "
            f"| {tier_status:>{widths[2]}} | {penalized_ratio:>{widths[3]}} |"
        )

    total = len(results)
    tiers = [sum(solved_tiers(result)[i] for result in results) for i in range(3)]
    average_ratio = sum(penalized_step_ratio(result) for result in results) / total if total else 0.0
    lines.extend([
        "",
        f"x5 solved: {tiers[0]}/{total}",
        f"x2 solved: {tiers[1]}/{total}",
        f"x1 solved: {tiers[2]}/{total}",
        f"average penalized step ratio: {average_ratio:.2f}",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_folder", type=Path, help="Run directory containing <alias>/server/server.log and per-game agent logs")
    args = parser.parse_args(argv)
    try:
        run_folder = args.run_folder.expanduser().resolve()
        # Refresh even when all generated files already exist; keep stdout Markdown-only.
        subprocess.run([sys.executable, str(ANALYSIS_PATH), str(run_folder)], check=True, stdout=sys.stderr)
        results = load_results(run_folder / RESULTS_FILENAME)
        total_cost_tokens = load_total_cost_tokens(run_folder / COST_TOKENS_FILENAME, {result.name for result in results})
        output = render_markdown(order_results(results)) + f"\ntotal cost tokens (M): {total_cost_tokens / 1_000_000:,.2f}"
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
