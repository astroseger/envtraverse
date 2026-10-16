"""Copy this package to implement an environment. Only standard-library dependencies."""

from __future__ import annotations

from typing import Any

from environment_api import EnvironmentError, EnvironmentStartError, InvalidActionError, Observation, StartResult


class EnvironmentManager:
    def __init__(self, parameters: dict[str, Any] | None = None) -> None:
        parameters = parameters or {}
        if set(parameters) - {"target"}:
            raise ValueError("The example environment only accepts the 'target' parameter")
        self.target = parameters.get("target", 3)
        if type(self.target) is not int or self.target <= 0:
            raise ValueError("target must be a positive integer")
        self.value: int | None = None
        self._used = False

    def start_environment(self, game_id: str) -> StartResult:
        if self._used:
            raise EnvironmentStartError("This environment has already been started or stopped")
        self._used = True
        if game_id != "counter":
            raise EnvironmentStartError(f"Unknown game_id {game_id!r}; use 'counter'")
        self.value = 0
        description = (
            "# Counter game\n\n"
            f"Reach {self.target} by incrementing a counter, starting from zero.\n\n"
            'Send {"id":"INC"} to increment or {"id":"RESET"} to reset to zero.\n'
            "The session ends when you reach the target.\n\n"
            "Each observation contains state.txt with the current value and target.\n"
        )
        return StartResult(environment_description=description, observation=self.observation())

    def observation(self) -> Observation:
        if self.value is None:
            raise EnvironmentError("Environment is not running")
        return Observation(
            files={"state.txt": f"value: {self.value}\ntarget: {self.target}\n"},
            terminal=self.value >= self.target,
        )

    def move(self, action: dict[str, Any]) -> Observation:
        if self.value is None:
            raise EnvironmentError("Environment is not running")
        if self.value >= self.target:
            raise InvalidActionError("The counter game is terminal")
        if not isinstance(action, dict) or action.get("id") not in ("INC", "RESET"):
            raise InvalidActionError('Use {"id":"INC"} or {"id":"RESET"}')
        self.value = self.value + 1 if action["id"] == "INC" else 0
        return self.observation()

    def stop(self) -> None:
        self._used = True
        self.value = None
