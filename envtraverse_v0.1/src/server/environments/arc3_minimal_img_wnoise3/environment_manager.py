from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
from typing import Any

from arc_agi import Arcade, OperationMode
from arcengine import GameAction
import numpy as np

from .artifacts_preprocessing import (
    ImageNoiseParameters,
    build_files,
    validate_image_format,
    validate_image_quality,
    validate_image_scale,
)
from environment_api import (
    EnvironmentError,
    EnvironmentStartError,
    FatalEnvironmentError,
    InvalidActionError,
    Observation,
    StartResult,
)

LOGGER = logging.getLogger("env-server.arc3")
DESCRIPTION_PATH = Path(__file__).resolve().parent / "environment_description.md"
SCORECARD_PATH = Path(os.environ.get("ENV_SCORECARD_PATH", "scorecard.txt"))
FINAL_SCORECARD_PATH = Path(os.environ.get("ENV_FINAL_SCORECARD_PATH", "final_scorecard.txt"))
MAX_STEPS_PER_LEVEL = 1500
WIN_STATE_NAME = "WIN"
GAME_OVER_STATE_NAME = "GAME_OVER"
OFFICIAL_ACTION_NAMES = frozenset(
    {"RESET", "ACTION1", "ACTION2", "ACTION3", "ACTION4", "ACTION5", "ACTION6", "ACTION7"}
)


@dataclass(frozen=True)
class EnvironmentManagerParameters:
    image_scale: int
    image_format: str
    image_quality: int
    image_noise: ImageNoiseParameters
    competition: bool = False
    source_url: str = "https://github.com/astroseger/arc-3-agents-baseline1"
    tags: list[str] = field(default_factory=lambda: ["api-test"])

    @classmethod
    def from_dict(cls, raw_parameters: dict[str, Any] | None) -> "EnvironmentManagerParameters":
        raw_parameters = {} if raw_parameters is None else raw_parameters
        if not isinstance(raw_parameters, dict):
            raise TypeError("Environment manager parameters must be a dictionary.")
        unknown = raw_parameters.keys() - {
            "competition", "source_url", "tags", "tag",
            "image_scale", "image_format", "image_quality", "image_noise",
        }
        if unknown:
            raise ValueError(f"Unknown environment manager parameters: {', '.join(sorted(repr(key) for key in unknown))}")
        if "tags" in raw_parameters and "tag" in raw_parameters:
            raise ValueError("Specify only one of 'tags' or 'tag'.")
        missing = {"image_scale", "image_format", "image_quality", "image_noise"} - raw_parameters.keys()
        if missing:
            raise ValueError(f"Missing required environment manager parameters: {', '.join(sorted(missing))}")

        competition = raw_parameters.get("competition", cls.competition)
        if type(competition) is not bool:
            raise TypeError("Environment manager parameter 'competition' must be a boolean.")
        source_url = raw_parameters.get("source_url", cls.source_url)
        if not isinstance(source_url, str):
            raise TypeError("Environment manager parameter 'source_url' must be a string.")
        source_url = source_url.strip()
        if not source_url:
            raise ValueError("Environment manager parameter 'source_url' must not be blank.")

        raw_tags = raw_parameters.get("tags", raw_parameters.get("tag", ["api-test"]))
        if isinstance(raw_tags, str):
            tags = [raw_tags]
        elif isinstance(raw_tags, list) and all(isinstance(tag, str) for tag in raw_tags):
            tags = raw_tags
        else:
            raise TypeError("Environment manager parameter 'tags' must be a string or list of strings.")

        tags = [tag.strip() for tag in tags if tag.strip()]
        if not tags:
            raise ValueError("Environment manager parameter 'tags' must contain at least one nonblank tag.")

        image_scale = validate_image_scale(raw_parameters["image_scale"])
        image_format = validate_image_format(raw_parameters["image_format"])
        image_quality = validate_image_quality(raw_parameters["image_quality"])
        image_noise = ImageNoiseParameters.from_dict(raw_parameters["image_noise"])
        image_noise.padding_for((64, 64), image_scale)

        return cls(
            competition=competition, source_url=source_url, tags=tags,
            image_scale=image_scale, image_format=image_format,
            image_quality=image_quality, image_noise=image_noise,
        )


def get_scorecard_id(scorecard: Any) -> str:
    if isinstance(scorecard, str):
        return scorecard
    if hasattr(scorecard, "card_id"):
        return str(scorecard.card_id)
    if hasattr(scorecard, "id"):
        return str(scorecard.id)
    raise TypeError(f"Cannot extract scorecard id from {scorecard!r}")


def write_text_atomic(path: Path, text: str) -> None:
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


def _game_action_from_name(action_name: str) -> GameAction:
    try:
        return GameAction[action_name]
    except KeyError:
        raise InvalidActionError("Invalid action: unknown action id") from None


def _coordinate_from_value(value: Any, coordinate_name: str) -> int:
    if isinstance(value, bool):
        raise InvalidActionError(f"Invalid action: ACTION6 {coordinate_name} must be an integer")

    if isinstance(value, int):
        coordinate = value
    elif isinstance(value, str):
        try:
            coordinate = int(value)
        except ValueError:
            raise InvalidActionError(f"Invalid action: ACTION6 {coordinate_name} must be an integer") from None
    else:
        raise InvalidActionError(f"Invalid action: ACTION6 {coordinate_name} must be an integer")

    if not 0 <= coordinate <= 63:
        raise InvalidActionError(
            f"Invalid action: ACTION6 {coordinate_name} must be inside the 64x64 frame (0..63)"
        )
    return coordinate


def _state_name(frame_data: Any) -> str:
    state = getattr(frame_data, "state", None)
    return state.name if hasattr(state, "name") else str(state)


def _levels_completed(frame_data: Any) -> int:
    return int(getattr(frame_data, "levels_completed", 0))


def parse_action(raw_action: dict[str, Any]) -> tuple[GameAction, Any | None, dict[str, Any] | None]:
    if not isinstance(raw_action, dict):
        raise InvalidActionError("Invalid action: action must be a JSON object")

    action_id = raw_action.get("id")
    if not isinstance(action_id, str) or not action_id.strip():
        raise InvalidActionError("Invalid action: action requires string id")
    action_id = action_id.strip()
    if action_id not in OFFICIAL_ACTION_NAMES:
        raise InvalidActionError("Invalid action: unknown action id")
    action = _game_action_from_name(action_id)

    action_input_override: dict[str, Any] = {"id": action_id}
    if action == GameAction.ACTION6:
        if "data" not in raw_action:
            raise InvalidActionError("Invalid action: ACTION6 requires data")
        raw_data = raw_action["data"]
        if not isinstance(raw_data, dict):
            raise InvalidActionError("Invalid action: ACTION6 data must be a JSON object")
        if "x" not in raw_data or "y" not in raw_data:
            raise InvalidActionError("Invalid action: ACTION6 requires both x and y")
        data = {
            "x": _coordinate_from_value(raw_data["x"], "x"),
            "y": _coordinate_from_value(raw_data["y"], "y"),
        }
        action_input_override["data"] = data
        return action, data, action_input_override

    if "data" in raw_action or "x" in raw_action or "y" in raw_action:
        raise InvalidActionError("Invalid action: coordinates are only valid with ACTION6")

    return action, None, action_input_override


class EnvironmentManager:
    def __init__(self, parameters: dict[str, Any] | None = None) -> None:
        self.parameters = EnvironmentManagerParameters.from_dict(parameters)
        # Fresh system entropy for each manager; no seed is exposed in parameters.
        self._image_rng = np.random.default_rng()
        self.arc: Arcade | None = None
        self.environment: Any = None
        self.scorecard_id: str | None = None
        self.state_name = ""
        self.current_level_steps = 0
        self.current_attempt_steps = 0
        self.levels_completed = 0
        self.terminal = False
        self._used = False

    def start_environment(self, game_id: str) -> StartResult:
        if self._used:
            raise EnvironmentStartError("This environment has already been started or stopped")
        self._used = True
        try:
            description = DESCRIPTION_PATH.read_text(encoding="utf-8")
            if not description.strip():
                raise ValueError(f"Environment description is empty: {DESCRIPTION_PATH}")
            self.arc = Arcade(operation_mode=OperationMode.COMPETITION) if self.parameters.competition else Arcade()
            if self.parameters.competition:
                if SCORECARD_PATH.exists():
                    raise FileExistsError(f"Refusing to start because scorecard file already exists: {SCORECARD_PATH}")
                scorecard = self.arc.create_scorecard(source_url=self.parameters.source_url, tags=self.parameters.tags)
                self.scorecard_id = get_scorecard_id(scorecard)
                write_text_atomic(SCORECARD_PATH, self.scorecard_id + "\n")
                LOGGER.info("opened scorecard: %s", self.scorecard_id)

            try:
                self.environment = (
                    self.arc.make(game_id, scorecard_id=self.scorecard_id)
                    if self.parameters.competition else self.arc.make(game_id)
                )
            except Exception as exc:
                raise EnvironmentStartError("Failed to create environment") from exc
            if self.environment is None:
                raise EnvironmentStartError("Environment could not be created")
            frame_data = self.environment.observation_space
            if frame_data is None:
                raise EnvironmentStartError("Environment returned no initial observation")
            self.state_name = _state_name(frame_data)
            self.levels_completed = _levels_completed(frame_data)
            self.terminal = self._terminal_for_frame(frame_data)
            return StartResult(
                environment_description=description,
                observation=Observation(
                    files=build_files(
                        frame_data,
                        image_scale=self.parameters.image_scale,
                        image_format=self.parameters.image_format,
                        image_quality=self.parameters.image_quality,
                        image_noise=self.parameters.image_noise,
                        rng=self._image_rng,
                    ),
                    terminal=self.terminal,
                ),
            )
        except BaseException:
            self.stop()
            raise

    def move(self, action: dict[str, Any]) -> Observation:
        if self.environment is None:
            raise EnvironmentError("Environment game is closed")
        if self.terminal:
            raise InvalidActionError("Invalid action: environment is terminal")

        game_action, data, action_input_override = parse_action(action)
        if self.state_name == GAME_OVER_STATE_NAME and game_action != GameAction.RESET:
            raise InvalidActionError("Invalid action: only RESET is allowed when the environment is in GAME_OVER")
        if game_action == GameAction.RESET and self.current_attempt_steps == 0:
            raise InvalidActionError("Invalid action: RESET is not allowed before taking an action in the current attempt")

        if game_action == GameAction.RESET:
            frame_data = self.environment.reset()
        else:
            frame_data = self.environment.step(game_action, data=data)

        if frame_data is None:
            raise FatalEnvironmentError("Environment returned no frame; the backend may have closed the session")

        new_levels_completed = _levels_completed(frame_data)
        if new_levels_completed != self.levels_completed:
            self.current_level_steps = 0
            self.levels_completed = new_levels_completed
            self.current_attempt_steps = 0
        else:
            self.current_level_steps += 1
            if game_action == GameAction.RESET:
                self.current_attempt_steps = 0
            else:
                self.current_attempt_steps += 1
        self.state_name = _state_name(frame_data)
        self.terminal = self._terminal_for_frame(frame_data)

        if getattr(frame_data, "action_input", None) is not None:
            action_input_override = None
        return Observation(
            files=build_files(
                frame_data, action_input_override,
                image_scale=self.parameters.image_scale,
                image_format=self.parameters.image_format,
                image_quality=self.parameters.image_quality,
                image_noise=self.parameters.image_noise,
                rng=self._image_rng,
            ),
            terminal=self.terminal,
        )

    def _terminal_for_frame(self, frame_data: Any) -> bool:
        return _state_name(frame_data) == WIN_STATE_NAME or self.current_level_steps > MAX_STEPS_PER_LEVEL

    def stop(self) -> None:
        self._used = True
        self.environment = None
        arc, self.arc = self.arc, None
        scorecard_id, self.scorecard_id = self.scorecard_id, None
        if arc is None or scorecard_id is None:
            return
        LOGGER.info("closing scorecard: %s", scorecard_id)
        try:
            final_scorecard = arc.close_scorecard(scorecard_id)
            write_text_atomic(FINAL_SCORECARD_PATH, str(final_scorecard))
            LOGGER.info("final scorecard written: %s", FINAL_SCORECARD_PATH)
        except Exception:
            LOGGER.exception("close_scorecard failed")
            LOGGER.error("scorecard id is saved in %s", SCORECARD_PATH)
