"""Play an ordered set of labyrinths, starting each one explicitly."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import json
import logging
from pathlib import Path
from typing import Any

from environment_api import (
    EnvironmentError,
    EnvironmentStartError,
    InvalidActionError,
    Observation,
    StartResult,
)
from ..agimaze_single.environment_manager import (
    EnvironmentManager as SingleEnvironmentManager,
    EnvironmentManagerParameters,
)

LOGGER = logging.getLogger("env-server.agimaze_set_v1")
ENVIRONMENT_DIR = Path(__file__).resolve().parent
SETS_DIR = ENVIRONMENT_DIR / "sets"
START_ACTION_ID = "start"
START_ACTION_JSON = '{"id":"start"}'

INITIAL_START_MESSAGE = (
    f"Please start the first labyrinth by sending the {START_ACTION_JSON} action.\n"
    f"Available actions: {START_ACTION_JSON}\n"
)
SOLVED_START_MESSAGE = (
    f"You have solved the labyrinth. Please start the next labyrinth by sending the {START_ACTION_JSON} action.\n"
    f"Available actions: {START_ACTION_JSON}\n"
)
FAILED_START_MESSAGE = (
    "You did not solve the labyrinth before reaching the maximum allowed number of actions. "
    "You have used five times as many actions as an average human typically needs to solve it. "
    "Consider what went wrong and what mistakes you made. "
    f"Please start the next labyrinth by sending the {START_ACTION_JSON} action.\n"
    f"Available actions: {START_ACTION_JSON}\n"
)
FINAL_SOLVED_MESSAGE = "You have solved the final labyrinth. The labyrinth set is complete.\n"
FINAL_FAILED_MESSAGE = (
    "You did not solve the final labyrinth before reaching the maximum allowed number of actions. "
    "You have used five times as many actions as an average human typically needs to solve it. "
    "The labyrinth set is complete.\n"
)


@dataclass(frozen=True)
class LabyrinthSpec:
    path: str
    tutorial: bool


class SequencePhase(Enum):
    AWAITING_START = "awaiting_start"
    PLAYING = "playing"
    TERMINAL = "terminal"


def load_labyrinth_set(game_id: str) -> tuple[LabyrinthSpec, ...]:
    available = {path.stem for path in SETS_DIR.glob("*.json") if path.is_file()}
    if game_id not in available:
        choices = ", ".join(sorted(available)) or "<none>"
        raise EnvironmentStartError(f"Unknown game_id {game_id!r}; available game_id values: {choices}")

    try:
        loaded = json.loads((SETS_DIR / f"{game_id}.json").read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EnvironmentStartError(f"Labyrinth set {game_id!r} must contain valid JSON") from exc
    if not isinstance(loaded, list):
        raise EnvironmentStartError(f"Labyrinth set {game_id!r} must be a JSON list")
    if not loaded:
        raise EnvironmentStartError(f"Labyrinth set {game_id!r} is empty")

    labyrinths = []
    for index, raw in enumerate(loaded):
        if not isinstance(raw, dict):
            raise EnvironmentStartError(f"Labyrinth set {game_id!r} entry {index} must be an object")
        raw_path = raw.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise EnvironmentStartError(f"Labyrinth set {game_id!r} entry {index} requires a non-empty path")
        tutorial = raw.get("tutorial", False)
        if not isinstance(tutorial, bool):
            raise EnvironmentStartError(f"Labyrinth set {game_id!r} entry {index} tutorial must be a boolean")
        labyrinths.append(LabyrinthSpec(path=raw_path.strip(), tutorial=tutorial))
    return tuple(labyrinths)


class EnvironmentManager:
    def __init__(self, parameters: dict[str, Any] | None = None) -> None:
        self.parameters = EnvironmentManagerParameters.from_dict(parameters)
        if not SETS_DIR.is_dir():
            raise FileNotFoundError(f"Sets folder does not exist: {SETS_DIR}")
        self.game_id = ""
        self.labyrinths: tuple[LabyrinthSpec, ...] = ()
        self.current_index = 0
        self.current_game: SingleEnvironmentManager | None = None
        self.phase = SequencePhase.AWAITING_START
        self._used = False
        self._closed = False

    def start_environment(self, game_id: str) -> StartResult:
        if self._used:
            raise EnvironmentStartError("This environment has already been started or stopped")
        self._used = True
        try:
            labyrinths = load_labyrinth_set(game_id)
            description = (ENVIRONMENT_DIR / "environment_description.md").read_text(encoding="utf-8")
            self.game_id = game_id
            self.labyrinths = labyrinths
            LOGGER.info("started labyrinth sequence game=%s labyrinths=%s; awaiting start action", game_id, len(labyrinths))
            return StartResult(
                environment_description=description,
                observation=Observation(files={"message.md": INITIAL_START_MESSAGE}),
            )
        except BaseException:
            self.stop()
            raise

    @property
    def terminal(self) -> bool:
        return self.phase is SequencePhase.TERMINAL

    def move(self, action: dict[str, Any]) -> Observation:
        if self._closed or not self.labyrinths:
            raise EnvironmentError("Environment is not running or is closed")
        if self.terminal:
            raise InvalidActionError("Invalid action: environment is terminal")
        if self.phase is SequencePhase.AWAITING_START:
            return self._start_labyrinth(action)
        if self.current_game is None:
            raise RuntimeError("Labyrinth set has no active labyrinth")

        observation = self.current_game.move(action)
        state = self.current_game.state()
        if observation.terminal:
            # A child is terminal both on success and on exceeding its action limit.
            return self._finish_current_labyrinth(solved=bool(state.get("done")))
        return Observation(files=self._current_files(state))

    def stop(self) -> None:
        self._used = True
        self._closed = True
        self._stop_current_labyrinth()

    def _start_labyrinth(self, action: dict[str, Any]) -> Observation:
        if not isinstance(action, dict):
            raise InvalidActionError("Invalid action: action must be a JSON object")
        action_id = action.get("id")
        if not isinstance(action_id, str) or not action_id.strip():
            raise InvalidActionError("Invalid action: action requires string id")
        if action_id.strip() != START_ACTION_ID:
            raise InvalidActionError(
                f"Invalid action: unknown action id {action_id.strip()}; available actions: {START_ACTION_ID}"
            )

        spec = self.labyrinths[self.current_index]
        game = SingleEnvironmentManager(asdict(self.parameters))
        try:
            result = game.start_environment(spec.path)
            files = self._current_files(game.state(), description=result.environment_description)
        except BaseException:
            game.stop()
            raise
        self.current_game = game
        self.phase = SequencePhase.PLAYING
        LOGGER.info("started requested labyrinth game=%s labyrinth_index=%s", self.game_id, self.current_index)
        return Observation(files=files)

    def _finish_current_labyrinth(self, *, solved: bool) -> Observation:
        finished_index = self.current_index
        self._stop_current_labyrinth()
        self.current_index += 1

        if self.current_index >= len(self.labyrinths):
            self.phase = SequencePhase.TERMINAL
            LOGGER.info(
                "finished labyrinth sequence game=%s final_labyrinth_index=%s solved=%s",
                self.game_id, finished_index, solved,
            )
            message = FINAL_SOLVED_MESSAGE if solved else FINAL_FAILED_MESSAGE
        else:
            self.phase = SequencePhase.AWAITING_START
            LOGGER.info(
                "finished labyrinth game=%s labyrinth_index=%s solved=%s; awaiting start for labyrinth_index=%s",
                self.game_id, finished_index, solved, self.current_index,
            )
            message = SOLVED_START_MESSAGE if solved else FAILED_START_MESSAGE
        return Observation(files={"message.md": message}, terminal=self.terminal)

    def _stop_current_labyrinth(self) -> None:
        if self.current_game is not None:
            self.current_game.stop()
            self.current_game = None

    def _current_files(self, state: dict[str, Any], *, description: str | None = None) -> dict[str, str | bytes]:
        content = json.dumps(
            {
                "text": str(state.get("text", "")),
                "inventory": state.get("inventory"),
                "labyrinth_index": self.current_index,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        files: dict[str, str | bytes] = {"state.txt": content + "\n"}
        if description is not None:
            files["labyrinth_description.md"] = description + "\n"
        if self.labyrinths[self.current_index].tutorial:
            files["map.txt"] = str(state.get("map", "")) + "\n"
        return files
