"""Shared environment interface. Implementations need no base class or HTTP code.

Exception messages defined here are returned to clients; keep them free of
private backend details. Chained causes may retain details for server-side logs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


# Adapters use this for blocking setup and game operations, including build commands.
# Readiness probes and process cleanup have separate, short timeouts.
ENVIRONMENT_OPERATION_TIMEOUT_SECONDS = 60 * 60.0


class InvalidActionError(ValueError):
    """An action was rejected without advancing the environment."""


class EnvironmentStartError(RuntimeError):
    """The requested game could not be started."""


class EnvironmentError(RuntimeError):
    """An environment operation failed without requiring server shutdown."""


class FatalEnvironmentError(EnvironmentError):
    """The game's backend is unusable; stop the manager and the server."""


@dataclass(frozen=True)
class Observation:
    files: dict[str, str | bytes]
    terminal: bool = False


@dataclass(frozen=True)
class StartResult:
    """The description and initial observation of the manager's single game."""

    environment_description: str
    observation: Observation


class EnvironmentManager(Protocol):
    def __init__(self, parameters: dict[str, Any]) -> None:
        """Store and validate configuration without starting a backend."""
        ...

    def start_environment(self, game_id: str) -> StartResult:
        """Attempt startup once per instance; clean up partial resources on failure."""
        ...

    def move(self, action: dict[str, Any]) -> Observation: ...

    def stop(self) -> None:
        """Release all resources, including after failed startup. Must be idempotent."""
        ...
