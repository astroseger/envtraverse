from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from typing import Any, Protocol


SERVER_LOG_RELATIVE_PATH = Path("server") / "server.log"
ACTIONS_FILENAME = "actions.json"
RESULTS_FILENAME = "results.json"
LABYRINTH_DIR = Path(__file__).resolve().parent.parent / "src/server/labyrinth"
LABYRINTH_PACKS_DIR = LABYRINTH_DIR / "packs"
LABYRINTH_SOURCE_DIR = LABYRINTH_DIR / "src"
DEFAULT_MAX_STEPS = 200

SESSION_STARTED_RE = re.compile(
    r"env-server\.server: started session "
    r"token=(?P<token>\S+) "
    r"requested_game_id=\S+ "
    r"game_id=(?P<game_id>\S+)\s*$"
)
ACTION_APPLIED_RE = re.compile(
    r"env-server\.server: applied action "
    r"session=(?P<token>\S+) "
    r"action=(?P<action>.+) "
    r"step_index=(?P<step_index>\d+)\s*$"
)

Action = dict[str, Any]
ActionResult = dict[str, Any]
ReplayResult = dict[str, Any]


@dataclass(frozen=True)
class ReplayOutcome:
    max_steps: int
    solved: bool


class Replayer(Protocol):
    def replay(self, labyrinth_name: str, actions: list[Action]) -> ReplayOutcome: ...


class LabyrinthReplayer:
    """Replay logged actions directly through the local labyrinth engine."""

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
            raise ValueError(
                f"Labyrinth path escapes packs directory: {labyrinth_name!r}"
            ) from exc
        if not labyrinth_path.is_file():
            raise FileNotFoundError(f"Labyrinth file does not exist: {labyrinth_path}")

        try:
            payload = json.loads(labyrinth_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Invalid JSON in labyrinth file {labyrinth_path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError(f"Labyrinth file {labyrinth_path} must contain an object")

        try:
            loaded = self._load_or_generate(payload)
        except Exception as exc:
            raise RuntimeError(
                f"Could not load labyrinth {labyrinth_name!r}: {exc}"
            ) from exc

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
        allowed_actions = set(
            self._available_actions(
                grenades_enabled=grenades_enabled,
                flashlight_enabled=flashlight_enabled,
            )
        )

        solved_at: int | None = None
        for action_number, action in enumerate(actions, start=1):
            action_id = action["id"].strip()
            if action_id not in allowed_actions:
                raise ValueError(
                    f"Invalid action {action_id!r} at action {action_number} for "
                    f"labyrinth {labyrinth_name!r}"
                )
            try:
                step = engine.step(action_id)
            except Exception as exc:
                raise RuntimeError(
                    f"Replay failed at action {action_number} ({action_id!r}) for "
                    f"labyrinth {labyrinth_name!r}: {exc}"
                ) from exc
            if "exited" in step.obs.events:
                solved_at = action_number
                break

        if solved_at is not None and solved_at != len(actions):
            raise ValueError(
                f"Labyrinth {labyrinth_name!r} was solved after {solved_at} actions, "
                f"but its log contains {len(actions)} actions"
            )

        metadata = loaded.config.get("metadata")
        raw_max_steps = metadata.get("max_steps") if isinstance(metadata, dict) else None
        max_steps = (
            raw_max_steps
            if isinstance(raw_max_steps, int)
            and not isinstance(raw_max_steps, bool)
            and raw_max_steps > 0
            else DEFAULT_MAX_STEPS
        )
        return ReplayOutcome(max_steps=max_steps, solved=solved_at is not None)


def _parse_action(raw_action: str, *, log_path: Path, line_number: int) -> Action:
    try:
        action = ast.literal_eval(raw_action)
    except (SyntaxError, ValueError) as exc:
        raise ValueError(
            f"Invalid action on line {line_number} of {log_path}: {raw_action!r}"
        ) from exc

    if not isinstance(action, dict):
        raise ValueError(
            f"Expected an action object on line {line_number} of {log_path}, "
            f"got {type(action).__name__}"
        )

    action_id = action.get("id")
    if not isinstance(action_id, str) or not action_id.strip():
        raise ValueError(
            f"Action on line {line_number} of {log_path} has no non-empty string id"
        )

    return action


def actions_by_game_from_server_log(log_path: Path) -> dict[str, list[Action]]:
    """Extract one action list per labyrinth, preserving session-start order."""
    token_to_game: dict[str, str] = {}
    actions_by_game: dict[str, list[Action]] = {}

    with log_path.open("r", encoding="utf-8", errors="replace") as log_file:
        for line_number, line in enumerate(log_file, start=1):
            if "env-server.server: started session " in line:
                match = SESSION_STARTED_RE.search(line)
                if match is None:
                    raise ValueError(
                        f"Could not parse started-session line {line_number} of "
                        f"{log_path}"
                    )

                token = match.group("token")
                game_id = match.group("game_id")
                if token in token_to_game:
                    raise ValueError(
                        f"Duplicate session token {token!r} on line {line_number} "
                        f"of {log_path}"
                    )
                if game_id in actions_by_game:
                    raise ValueError(
                        f"Multiple sessions found for labyrinth {game_id!r} in "
                        f"{log_path}"
                    )

                token_to_game[token] = game_id
                actions_by_game[game_id] = []
                continue

            if "env-server.server: applied action " not in line:
                continue

            match = ACTION_APPLIED_RE.search(line)
            if match is None:
                raise ValueError(
                    f"Could not parse applied-action line {line_number} of {log_path}"
                )

            token = match.group("token")
            game_id = token_to_game.get(token)
            if game_id is None:
                raise ValueError(
                    f"Action on line {line_number} of {log_path} refers to unknown "
                    f"session token {token!r}"
                )

            expected_step_index = len(actions_by_game[game_id]) + 1
            step_index = int(match.group("step_index"))
            if step_index != expected_step_index:
                raise ValueError(
                    f"Unexpected step_index {step_index} on line {line_number} of "
                    f"{log_path} for labyrinth {game_id!r}; expected "
                    f"{expected_step_index}"
                )

            actions_by_game[game_id].append(
                _parse_action(
                    match.group("action"),
                    log_path=log_path,
                    line_number=line_number,
                )
            )

    if not actions_by_game:
        raise ValueError(f"No started sessions found in {log_path}")

    return actions_by_game


def action_results_for_games(
    actions_by_game: dict[str, list[Action]],
) -> list[ActionResult]:
    return [
        {"name": labyrinth_name, "actions": actions}
        for labyrinth_name, actions in actions_by_game.items()
    ]


def replay_results(
    action_results: list[ActionResult],
    *,
    replayer: Replayer | None = None,
) -> list[ReplayResult]:
    """Replay all labyrinths and build action-free result records."""
    replayer = replayer or LabyrinthReplayer()
    results: list[ReplayResult] = []

    for labyrinth_index, labyrinth in enumerate(action_results):
        name = labyrinth.get("name")
        actions = labyrinth.get("actions")
        if not isinstance(name, str) or not isinstance(actions, list):
            raise ValueError(f"Invalid action result: {labyrinth!r}")

        outcome = replayer.replay(name, actions)
        results.append(
            {
                "name": name,
                "labyrinth_index": labyrinth_index,
                "steps": len(actions),
                "max_steps": outcome.max_steps,
                "solved": outcome.solved,
            }
        )

    return results


def _write_json(path: Path, payload: list[dict[str, Any]]) -> Path:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def write_action_results(run_folder: Path, results: list[ActionResult]) -> Path:
    return _write_json(run_folder / ACTIONS_FILENAME, results)


def write_replay_results(run_folder: Path, results: list[ReplayResult]) -> Path:
    return _write_json(run_folder / RESULTS_FILENAME, results)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract one action list per labyrinth from "
            "<run_folder>/server/server.log, replay every action list through the "
            "local labyrinth engine, and write actions.json plus results.json."
        )
    )
    parser.add_argument("run_folder", type=Path, help="Completed run folder")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    run_folder = args.run_folder.expanduser().resolve()
    if not run_folder.is_dir():
        print(f"Run folder does not exist: {run_folder}", file=sys.stderr)
        return 1

    log_path = run_folder / SERVER_LOG_RELATIVE_PATH
    if not log_path.is_file():
        print(f"Server log does not exist: {log_path}", file=sys.stderr)
        return 1

    try:
        actions_by_game = actions_by_game_from_server_log(log_path)
        action_results = action_results_for_games(actions_by_game)
        results = replay_results(action_results)
        actions_path = write_action_results(run_folder, action_results)
        results_path = write_replay_results(run_folder, results)
    except (
        FileNotFoundError,
        ImportError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"Analysis failed: {exc}", file=sys.stderr)
        return 1

    action_count = sum(len(entry["actions"]) for entry in action_results)
    solved_count = sum(bool(entry["solved"]) for entry in results)
    print(
        f"wrote {actions_path} "
        f"({len(action_results)} labyrinths, {action_count} actions)"
    )
    print(f"wrote {results_path} ({solved_count}/{len(results)} solved)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
