#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import sys
from typing import Any


RESULTS_FILENAME = "results.json"
ORDER_PATH = Path(__file__).resolve().with_name("order.json")


@dataclass(frozen=True)
class LabyrinthResult:
    name: str
    steps: int
    max_steps: int
    solved: bool


class ResultOrderingError(ValueError):
    """Raised when results cannot be ordered using order.json."""


def results_path_for_run_folder(run_folder: Path) -> Path:
    return run_folder / RESULTS_FILENAME


def _required_int(
    entry: dict[str, Any],
    key: str,
    *,
    entry_index: int,
    results_path: Path,
    minimum: int,
) -> int:
    value = entry.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        comparison = "positive" if minimum == 1 else "non-negative"
        raise ValueError(
            f"Entry {entry_index} in {results_path} requires a {comparison} integer {key!r}"
        )
    return value


def load_results(results_path: Path) -> list[LabyrinthResult]:
    try:
        payload = json.loads(results_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {results_path}: {exc}") from exc

    if not isinstance(payload, list):
        raise ValueError(f"Expected a list in results file {results_path}")

    results: list[LabyrinthResult] = []
    for entry_index, entry in enumerate(payload):
        if not isinstance(entry, dict):
            raise ValueError(f"Entry {entry_index} in {results_path} must be an object")

        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f"Entry {entry_index} in {results_path} requires a non-empty string 'name'"
            )

        solved = entry.get("solved")
        if not isinstance(solved, bool):
            raise ValueError(
                f"Entry {entry_index} in {results_path} requires a boolean 'solved'"
            )

        results.append(
            LabyrinthResult(
                name=name.strip(),
                steps=_required_int(
                    entry,
                    "steps",
                    entry_index=entry_index,
                    results_path=results_path,
                    minimum=0,
                ),
                max_steps=_required_int(
                    entry,
                    "max_steps",
                    entry_index=entry_index,
                    results_path=results_path,
                    minimum=1,
                ),
                solved=solved,
            )
        )

    return results


def load_result_order(order_path: Path = ORDER_PATH) -> dict[str, int]:
    try:
        payload = json.loads(order_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ResultOrderingError(
            f"Could not read labyrinth ordering file {order_path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ResultOrderingError(
            f"Invalid JSON in labyrinth ordering file {order_path}: {exc}"
        ) from exc

    if not isinstance(payload, list):
        raise ResultOrderingError(
            f"Labyrinth ordering file {order_path} must contain a JSON list"
        )

    names: list[str] = []
    for entry_index, name in enumerate(payload):
        if not isinstance(name, str) or not name.strip():
            raise ResultOrderingError(
                f"Entry {entry_index} in {order_path} must be a non-empty string"
            )
        names.append(name.strip())

    duplicates = sorted(
        name for name, count in Counter(names).items() if count > 1
    )
    if duplicates:
        duplicate_lines = "\n".join(f"  - {name}" for name in duplicates)
        raise ResultOrderingError(
            f"Duplicate labyrinth names in {order_path}:\n{duplicate_lines}"
        )

    return {name: index for index, name in enumerate(names)}


def labyrinth_order_key(result_name: str) -> str:
    """Return the labyrinth path without its variable JSON filename."""
    return PurePosixPath(result_name).parent.as_posix()


def order_results(
    results: list[LabyrinthResult],
    order_path: Path = ORDER_PATH,
) -> list[LabyrinthResult]:
    result_order = load_result_order(order_path)
    result_key_counts = Counter(
        labyrinth_order_key(result.name) for result in results
    )
    missing_names = sorted(
        result.name
        for result in results
        if labyrinth_order_key(result.name) not in result_order
    )
    duplicate_keys = sorted(
        key for key, count in result_key_counts.items() if count > 1
    )

    problems: list[str] = []
    if missing_names:
        missing_lines = "\n".join(f"  - {name}" for name in missing_names)
        problems.append(
            f"Labyrinth names not found in {order_path}:\n{missing_lines}"
        )
    if duplicate_keys:
        duplicate_lines = "\n".join(f"  - {key}" for key in duplicate_keys)
        problems.append(
            f"Duplicate labyrinth paths in {RESULTS_FILENAME} after ignoring "
            f"JSON filenames:\n{duplicate_lines}"
        )
    if problems:
        raise ResultOrderingError("\n".join(problems))

    return sorted(
        results,
        key=lambda result: result_order[labyrinth_order_key(result.name)],
    )


def markdown_cell(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "<br>")
    )


def solved_tiers(result: LabyrinthResult) -> tuple[int, int, int]:
    return (
        int(result.solved),
        int(result.solved and result.steps <= 2 * result.max_steps),
        int(result.solved and result.steps <= result.max_steps),
    )


def score1(result: LabyrinthResult) -> float:
    if not result.solved:
        return 0.0
    overrun = max(0.0, result.steps / result.max_steps - 1.0)
    return 1.0 / (1.0 + overrun**2)


def render_markdown(results: list[LabyrinthResult]) -> str:
    headers = (
        "name",
        "steps/max_steps",
        "x5/x2/x1 solved",
        "score1",
    )
    rows = [
        (
            markdown_cell(result.name),
            f"{result.steps}/{result.max_steps}",
            "/".join(str(value) for value in solved_tiers(result)),
            f"{score1(result):.3f}",
        )
        for result in results
    ]

    widths = tuple(
        max(len(header), *(len(row[column]) for row in rows))
        for column, header in enumerate(headers)
    )
    separators = (
        "-" * widths[0],
        "-" * (widths[1] - 1) + ":",
        "-" * (widths[2] - 1) + ":",
        "-" * (widths[3] - 1) + ":",
    )
    lines = [
        (
            "Score1: unsolved labyrinths score `0`. For solved labyrinths, "
            "`overrun = max(0, steps/max_steps - 1)` and "
            "`score1 = 1/(1 + overrun^2)`; solutions within `max_steps` score `1`."
        ),
        "",
        (
            f"| {headers[0]:<{widths[0]}} | {headers[1]:<{widths[1]}} "
            f"| {headers[2]:<{widths[2]}} | {headers[3]:<{widths[3]}} |"
        ),
        (
            f"| {separators[0]} | {separators[1]} "
            f"| {separators[2]} | {separators[3]} |"
        ),
    ]

    for name, step_ratio, tier_status, result_score1 in rows:
        lines.append(
            f"| {name:<{widths[0]}} | {step_ratio:>{widths[1]}} "
            f"| {tier_status:>{widths[2]}} | {result_score1:>{widths[3]}} |"
        )

    total = len(results)
    x5_solved = sum(result.solved for result in results)
    x2_solved = sum(
        result.solved and result.steps <= 2 * result.max_steps for result in results
    )
    x1_solved = sum(
        result.solved and result.steps <= result.max_steps for result in results
    )
    total_actions = sum(result.steps for result in results)
    average_score1 = sum(score1(result) for result in results) / total if total else 0.0
    lines.extend(
        [
            "",
            f"x5 solved: {x5_solved}/{total}",
            f"x2 solved: {x2_solved}/{total}",
            f"x1 solved: {x1_solved}/{total}",
            f"total actions: {total_actions}",
            f"average score1: {average_score1:.3f}",
        ]
    )
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read results.json from a completed run folder and print its labyrinth "
            "results as a Markdown table ordered by analysis_scripts/order.json."
        )
    )
    parser.add_argument(
        "run_folder",
        type=Path,
        help="Run folder containing results.json",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    run_folder = args.run_folder.expanduser().resolve()
    if not run_folder.is_dir():
        print(f"Run folder does not exist: {run_folder}", file=sys.stderr)
        return 1

    try:
        results_path = results_path_for_run_folder(run_folder)
        if not results_path.is_file():
            raise FileNotFoundError(f"Results file does not exist: {results_path}")
        results = load_results(results_path)
        results = order_results(results)
    except ResultOrderingError as exc:
        print(f"Critical error: {exc}", file=sys.stderr)
        return 1
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(render_markdown(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
