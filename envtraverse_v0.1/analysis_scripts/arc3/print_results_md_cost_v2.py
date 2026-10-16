#!/usr/bin/env python3
"""Print ARC3 results with cost tokens in millions from scorecards and raw_cost_tokens.json only."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import sys
from typing import Any


SCORECARD_SUFFIX = "_scorecard.json"


@dataclass(frozen=True)
class GameResult:
    environment_id: str
    run_index: str
    levels_solved: int
    total_levels: int
    score: float
    steps_solved: int
    total_actions: int
    cost_tokens: float

    @property
    def fully_solved(self) -> bool:
        return self.total_levels > 0 and self.levels_solved == self.total_levels

    def table_row(self) -> list[str]:
        return [
            self.environment_id,
            self.run_index,
            f"{self.levels_solved}/{self.total_levels}",
            f"{self.score:.2f}%",
            str(self.steps_solved),
            str(self.total_actions),
            f"{self.cost_tokens / 1_000_000:,.2f}",
        ]


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc


def nonnegative_integer(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def cost_tokens(tokens: dict[str, Any]) -> float:
    input_tokens = nonnegative_integer(tokens.get("input_tokens"), "input_tokens")
    cached_tokens = nonnegative_integer(tokens.get("cached_input_tokens"), "cached_input_tokens")
    output_tokens = nonnegative_integer(tokens.get("output_tokens"), "output_tokens")
    if cached_tokens > input_tokens:
        raise ValueError("cached_input_tokens cannot exceed input_tokens")
    # Normalize to one output token; cached input is included in input_tokens.
    return cached_tokens / 60 + (input_tokens - cached_tokens) / 6 + output_tokens


def read_cost_tokens(path: Path) -> dict[str, dict[str, Any]]:
    records = read_json(path)
    if not isinstance(records, list):
        raise ValueError(f"{path}: expected a list of token records")
    by_game = {}
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValueError(f"{path}: record {index} must be an object")
        game = record.get("environment_id")
        if not isinstance(game, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", game):
            raise ValueError(f"{path}: record {index} has an invalid environment_id")
        if game in by_game:
            raise ValueError(f"{path}: duplicate environment_id {game!r}")
        by_game[game] = record
    return by_game


def result_from_scorecard(path: Path, game: str, run_index: str, cost: float) -> GameResult:
    card = read_json(path)
    if not isinstance(card, dict):
        raise ValueError("expected a scorecard object")
    environments = card.get("environments")
    if not isinstance(environments, list) or len(environments) != 1 or not isinstance(environments[0], dict):
        raise ValueError("expected exactly one environment in the scorecard")
    runs = environments[0].get("runs")
    if not isinstance(runs, list) or len(runs) != 1 or not isinstance(runs[0], dict):
        raise ValueError("expected exactly one run in the scorecard environment")
    run = runs[0]
    levels_solved = nonnegative_integer(card.get("total_levels_completed"), "total_levels_completed")
    total_levels = nonnegative_integer(card.get("total_levels"), "total_levels")
    total_actions = nonnegative_integer(card.get("total_actions"), "total_actions")
    if levels_solved > total_levels:
        raise ValueError("total_levels_completed cannot exceed total_levels")
    score = card.get("score")
    if type(score) not in (int, float) or not math.isfinite(score):
        raise ValueError("score must be a finite number")
    if not isinstance(run.get("state"), str):
        raise ValueError("run state must be a string")
    completed = nonnegative_integer(run.get("levels_completed"), "run levels_completed")
    if completed != levels_solved:
        raise ValueError("run levels_completed does not match total_levels_completed")

    level_actions = run.get("level_actions")
    if isinstance(level_actions, list):
        counts = [nonnegative_integer(value, "level_actions entry") for value in level_actions]
        steps_solved = sum(counts[:completed])
    elif level_actions is None:
        steps_solved = nonnegative_integer(run.get("actions"), "run actions") if run["state"] == "WIN" else 0
    else:
        raise ValueError("level_actions must be a list or null")

    if not level_actions:
        nonnegative_integer(run.get("actions"), "run actions")
    return GameResult(
        environment_id=game, run_index=run_index, levels_solved=levels_solved,
        total_levels=total_levels, score=float(score), steps_solved=steps_solved,
        total_actions=total_actions, cost_tokens=cost,
    )


def results_for_runs_dir(runs_dir: Path, run_index: str) -> list[GameResult]:
    tokens_path = runs_dir / "raw_cost_tokens.json"
    tokens = read_cost_tokens(tokens_path)
    scorecard_games = {path.name[:-len(SCORECARD_SUFFIX)] for path in runs_dir.glob(f"*{SCORECARD_SUFFIX}")}
    missing_scorecards = tokens.keys() - scorecard_games
    missing_tokens = scorecard_games - tokens.keys()
    if missing_scorecards:
        raise ValueError(f"{runs_dir}: missing scorecards for {', '.join(sorted(missing_scorecards))}; run generate_scorecards_by_replay.py first")
    if missing_tokens:
        raise ValueError(f"{tokens_path}: missing token records for {', '.join(sorted(missing_tokens))}; run extract_raw_cost_tokens.py first")

    results = []
    for game, token_record in sorted(tokens.items()):
        try:
            cost = cost_tokens(token_record)
        except ValueError as exc:
            raise ValueError(f"{tokens_path}: {game}: {exc}") from exc
        scorecard_path = runs_dir / f"{game}{SCORECARD_SUFFIX}"
        try:
            results.append(result_from_scorecard(scorecard_path, game, run_index, cost))
        except ValueError as exc:
            raise ValueError(f"{scorecard_path}: {exc}") from exc
    return results


def print_summary(results: list[GameResult]) -> None:
    by_game: dict[str, list[GameResult]] = {}
    for result in results:
        by_game.setdefault(result.environment_id, []).append(result)
    fully_solved = sum(all(result.fully_solved for result in game_results) for game_results in by_game.values())
    per_game_scores = [
        sum(result.score for result in game_results) / len(game_results)
        for game_results in by_game.values()
    ]
    mean_score = sum(per_game_scores) / len(per_game_scores) if per_game_scores else 0.0
    print()
    print(f"fully solved games: {fully_solved}/{len(by_game)}")
    print(f"mean per-game RHAE: {mean_score:.2f}%")
    print(f"total cost tokens (M): {sum(result.cost_tokens for result in results) / 1_000_000:,.2f}")
    print(f"total number of actions: {sum(result.total_actions for result in results)}")


def print_results_md(runs_dirs: list[Path]) -> None:
    # The new layout ends in <experiment>/run, so use the experiment name when
    # distinguishing repeated games across runs, not the per-game alias folder.
    labels = [path.parent.name if path.name == "run" else path.name for path in runs_dirs]
    results = []
    for runs_dir, label in zip(runs_dirs, labels):
        run_index = str(runs_dir) if labels.count(label) > 1 else label
        results.extend(results_for_runs_dir(runs_dir, run_index))

    # Load and validate every input before printing, avoiding partial reports.
    rows = sorted(result.table_row() for result in results)
    show_run_index = len({result.environment_id for result in results}) != len(results)
    print("# Results")
    print()
    if show_run_index:
        print("| Game | Run index | Levels solved | Score | Steps on Solved | Total Steps | Cost tokens (M) |")
        print("|---|---:|---:|---:|---:|---:|---:|")
    else:
        print("| Game | Levels solved | Score | Steps on Solved | Total Steps | Cost tokens (M) |")
        print("|---|---:|---:|---:|---:|---:|")
        rows = [[row[0], *row[2:]] for row in rows]
    for row in rows:
        print("| " + " | ".join(cell.replace("|", "\\|").replace("\n", " ").replace("\r", " ") for cell in row) + " |")
    print_summary(results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs_dirs", nargs="+", type=Path, help="One or more run folders containing scorecards and raw_cost_tokens.json")
    args = parser.parse_args(argv)
    try:
        runs_dirs = [path.expanduser().resolve() for path in args.runs_dirs]
        if len(set(runs_dirs)) != len(runs_dirs):
            raise ValueError("Specify each run folder only once")
        for runs_dir in runs_dirs:
            if not runs_dir.is_dir():
                raise ValueError(f"Run folder does not exist: {runs_dir}")
        print_results_md(runs_dirs)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
