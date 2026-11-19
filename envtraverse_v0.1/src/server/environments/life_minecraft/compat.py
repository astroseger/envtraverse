"""Use the server's actual API classes; allow standalone client/smoke usage."""
from __future__ import annotations

try:
    from environment_api import (  # type: ignore[import-not-found]
        ENVIRONMENT_OPERATION_TIMEOUT_SECONDS, EnvironmentError, EnvironmentStartError, FatalEnvironmentError, InvalidActionError,
        Observation, StartResult,
    )
except ModuleNotFoundError as exc:
    if exc.name != "environment_api":
        raise
    from dataclasses import dataclass
    from typing import Any

    class EnvironmentError(RuntimeError):
        """Standalone counterpart of the Unified Agent exception."""

    ENVIRONMENT_OPERATION_TIMEOUT_SECONDS = 60 * 60.0

    class FatalEnvironmentError(EnvironmentError):
        """The single-game backend is unavailable."""

    class EnvironmentStartError(RuntimeError):
        """Standalone counterpart of the Unified Agent exception."""

    class InvalidActionError(ValueError):
        """Standalone counterpart of the Unified Agent exception."""

    @dataclass(frozen=True)
    class Observation:
        files: dict[str, str | bytes]
        terminal: bool = False

    @dataclass(frozen=True)
    class StartResult:
        environment_description: str
        observation: Observation
