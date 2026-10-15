#!/usr/bin/env python3
"""Refresh raw actions and token counts, replay the mazes, and write results.json."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Protocol


ROOT = Path(__file__).resolve().parents[2]
EXTRACTOR_PATHS = tuple(
    ROOT / "analysis_scripts" / name
    for name in ("extract_raw_actions.py", "extract_raw_cost_tokens.py")
)
LABYRINTH_DIR = ROOT / "src/server/environments/agimaze_single/labyrinth"
LABYRINTH_PACKS_DIR = LABYRINTH_DIR / "packs"
LABYRINTH_SOURCE_DIR = LABYRINTH_DIR / "src"
DEFAULT_MAX_STEPS = 200
RESULTS_FILENAME = "results.json"
Action = dict[str, Any]


@dataclass(frozen=True)
class ReplayOutcome:
    max_steps: int
    solved: bool


class Replayer(Protocol):
    def replay(self, labyrinth_name: str, actions: list[Action]) -> ReplayOutcome: ...


class LabyrinthReplayer:
    """Replay through the same local engine and game definitions as the server."""

    def __init__(self, packs_dir: Path = LABYRINTH_PACKS_DIR) -> None:
        self.packs_dir = packs_dir.resolve()
        source_dir = str(LABYRINTH_SOURCE_DIR)
        if source_dir not in sys.path:
            sys.path.insert(0, source_dir)

        from labyrinth.engine import Engine, available_actions
        from labyrinth.loader import load_or_generate

        self._engine_class = Engine
        self._available_actions = available_actions
        self._load_or_generate = load_or_generate

    def replay(self, labyrinth_name: str, actions: list[Action]) -> ReplayOutcome:
        labyrinth_path = (self.packs_dir / labyrinth_name).resolve()
        try:
            labyrinth_path.relative_to(self.packs_dir)
        except ValueError as exc:
            raise ValueError(f"Labyrinth path escapes packs directory: {labyrinth_name!r}") from exc
        if not labyrinth_path.is_file():
            raise FileNotFoundError(f"Labyrinth file does not exist: {labyrinth_path}")

        payload = json.loads(labyrinth_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Labyrinth file {labyrinth_path} must contain an object")
        loaded = self._load_or_generate(payload)
        generated = loaded.generated
        systems = generated.config.systems
        flashlight = getattr(systems, "flashlight", None)
        flashlight_enabled = bool(getattr(flashlight, "enabled", False))
        grenades_enabled = bool(systems.grenades.enabled)

        engine = self._engine_class(
            generated.board,
            generated.start,
            lang="en",
            require_exit_key_to_leave=generated.goal.require_exit_key_to_leave,
            grenades_enabled=grenades_enabled,
            boats_enabled=bool(systems.boats.enabled),
            flashlight_enabled=flashlight_enabled,
            flashlight_charges=int(getattr(flashlight, "charges", 0) or 0),
        )
        allowed_actions = set(self._available_actions(
            grenades_enabled=grenades_enabled,
            flashlight_enabled=flashlight_enabled,
        ))

        solved = False
        for action_number, action in enumerate(actions, start=1):
            action_id = action["id"].strip()
            if action_id not in allowed_actions:
                raise ValueError(f"Invalid action {action_id!r} at action {action_number}")
            try:
                step = engine.step(action_id)
            except Exception as exc:
                raise RuntimeError(f"Replay failed at action {action_number} ({action_id!r}): {exc}") from exc
            if "exited" in step.obs.events:
                if action_number != len(actions):
                    raise ValueError(
                        f"Labyrinth was solved after {action_number} actions, "
                        f"but its log contains {len(actions)} actions"
                    )
                solved = True

        # Keep the original (x1) budget for scoring, not the server's x5 cap.
        metadata = loaded.config.get("metadata")
        raw_max_steps = metadata.get("max_steps") if isinstance(metadata, dict) else None
        max_steps = raw_max_steps if type(raw_max_steps) is int and raw_max_steps > 0 else DEFAULT_MAX_STEPS
        return ReplayOutcome(max_steps=max_steps, solved=solved)


def read_raw_actions(path: Path) -> list[dict[str, Any]]:
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError(f"{path}: expected a list of game records")
    seen_ids: set[str] = set()
    for index, record in enumerate(records, start=1):
        location = f"{path}: record {index}"
        if not isinstance(record, dict):
            raise ValueError(f"{location} must be an object")
        environment_id = record.get("environment_id")
        if not isinstance(environment_id, str) or not environment_id.strip():
            raise ValueError(f"{location} requires a non-empty string environment_id")
        if environment_id in seen_ids:
            raise ValueError(f"{path}: duplicate environment_id {environment_id!r}")
        seen_ids.add(environment_id)
        actions = record.get("actions")
        if not isinstance(actions, list):
            raise ValueError(f"{location} requires an actions list")
        for action_number, action in enumerate(actions, start=1):
            if not isinstance(action, dict):
                raise ValueError(f"{location}, action {action_number} must be an object")
            action_id = action.get("id")
            if not isinstance(action_id, str) or not action_id.strip():
                raise ValueError(f"{location}, action {action_number} requires a non-empty string id")
    return records


def replay_results(
    records: list[dict[str, Any]], *, replayer: Replayer | None = None,
) -> list[dict[str, Any]]:
    if not records:
        return []
    if replayer is None:
        replayer = LabyrinthReplayer()
    results = []
    for index, record in enumerate(records):
        name = record["environment_id"]
        actions = record["actions"]
        try:
            outcome = replayer.replay(name, actions)
        except Exception as exc:
            raise RuntimeError(f"Could not replay {name!r}: {exc}") from exc
        results.append({
            "name": name,
            "labyrinth_index": index,
            "steps": len(actions),
            "max_steps": outcome.max_steps,
            "solved": outcome.solved,
        })
    return results


def write_results(path: Path, results: list[dict[str, Any]]) -> None:
    # Only replace a previous report after every game has replayed successfully.
    output = json.dumps(results, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
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
    parser.add_argument("run_folder", type=Path, help="Run directory containing <alias>/server/server.log and per-game agent logs")
    args = parser.parse_args(argv)
    try:
        run_folder = args.run_folder.expanduser().resolve()
        for extractor_path in EXTRACTOR_PATHS:
            subprocess.run([sys.executable, str(extractor_path), str(run_folder)], check=True)
        records = read_raw_actions(run_folder / "raw_actions.json")
        results = replay_results(records)
        results_path = run_folder / RESULTS_FILENAME
        write_results(results_path, results)
    except subprocess.CalledProcessError as exc:
        print(f"Error: {extractor_path.name} failed; results.json was not updated.", file=sys.stderr)
        return exc.returncode if exc.returncode > 0 else 1
    except (OSError, UnicodeError, ImportError, RuntimeError, TypeError, ValueError) as exc:
        print(f"Analysis failed: {exc}; results.json was not updated.", file=sys.stderr)
        return 1

    solved_count = sum(result["solved"] for result in results)
    action_count = sum(result["steps"] for result in results)
    print(f"Wrote {results_path} ({solved_count}/{len(results)} solved, {action_count} actions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
