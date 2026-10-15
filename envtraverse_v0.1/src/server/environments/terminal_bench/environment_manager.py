"""Single-game Unified Agent manager. Standard library only; no host startup in __init__."""
from __future__ import annotations

import logging
import math
import os
from typing import Any
from uuid import uuid4

from .actions import validate_action
from .compat import (
    ENVIRONMENT_OPERATION_TIMEOUT_SECONDS, EnvironmentError, EnvironmentStartError,
    FatalEnvironmentError, InvalidActionError, Observation, StartResult,
)
from .protocol import RuntimeClient, RuntimeFault, decode_observation
from .records import session_key

LOGGER = logging.getLogger("env-server.terminal_bench")


def _observation(raw: dict[str, Any]) -> Observation:
    files, terminal = decode_observation(raw)
    return Observation(files=files, terminal=terminal)


class EnvironmentManager:
    def __init__(self, parameters: dict[str, Any] | None = None):
        parameters = {} if parameters is None else parameters
        allowed = {"socket_path", "request_timeout_seconds", "session_id"}
        if not isinstance(parameters, dict) or set(parameters) - allowed:
            raise ValueError("Expected socket_path, optional session_id and request_timeout_seconds")
        path = parameters.get("socket_path", os.environ.get("TB_RUNTIME_SOCKET", "/run/terminal-bench/runtime.sock"))
        if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
            raise ValueError("socket_path must be an absolute pathname")
        timeout = parameters.get("request_timeout_seconds", ENVIRONMENT_OPERATION_TIMEOUT_SECONDS)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        self.session_id = session_key(parameters.get("session_id", uuid4().hex))
        # Private per-manager capability: failed duplicate startup must not close another manager's session.
        self._owner_token = uuid4().hex
        self.client = RuntimeClient(path, timeout=timeout)
        self._used = False
        self._started = False
        self._start_sent = False
        self._stopped = False
        self._terminal = False
        # Deliberately no health check, socket connection, worker, or Harbor import here.

    def _call(self, method: str, **parameters: Any) -> dict[str, Any]:
        return self.client.call(method, session_id=self.session_id,
                                owner_token=self._owner_token, **parameters)

    def start_environment(self, game_id: str) -> StartResult:
        if self._used:
            raise EnvironmentStartError("This environment has already been started or stopped")
        self._used = True
        LOGGER.info("Starting Terminal-Bench session_id=%s game_id=%s", self.session_id, game_id)
        try:
            if not isinstance(game_id, str) or not game_id.strip():
                raise ValueError("game_id must be a nonempty string")
            self._start_sent = True
            raw = self._call("start", game_id=game_id)
            if raw.get("session_id") != self.session_id:
                raise ValueError("Runtime returned a different session ID")
            description = raw["environment_description"]
            if not isinstance(description, str) or not description.strip():
                raise ValueError("Runtime returned an empty description")
            initial = _observation(raw["observation"])
            self._started, self._terminal = True, initial.terminal
            return StartResult(environment_description=description, observation=initial)
        except (OSError, ValueError, KeyError, RuntimeFault) as exc:
            if isinstance(exc, RuntimeFault) and exc.code in {"duplicate_session", "not_owner"}:
                self._start_sent = False  # No authority over the already existing execution.
            try:
                self.stop()
            except EnvironmentError:
                LOGGER.exception("Startup cleanup failed for session_id=%s; inspect runtime results", self.session_id)
            message = str(exc) if isinstance(exc, RuntimeFault) else "Runtime preparation failed"
            raise EnvironmentStartError(message) from exc

    def move(self, action: dict[str, Any]) -> Observation:
        if not self._started or self._stopped:
            raise EnvironmentError("Environment is not running")
        if self._terminal:
            raise InvalidActionError("The environment is terminal")
        try:
            validate_action(action)
            raw = self._call("action", action=action)
            result = _observation(raw["observation"])
        except RuntimeFault as exc:
            if exc.code in {"invalid_action", "bad_request", "unknown_terminal", "terminal"}:
                raise InvalidActionError(str(exc)) from exc
            if exc.code in {"worker_failed", "unknown_session", "not_ready", "unavailable", "not_owner"}:
                raise FatalEnvironmentError(str(exc)) from exc
            raise EnvironmentError(str(exc)) from exc
        except (OSError, ValueError, KeyError) as exc:
            raise FatalEnvironmentError("Runtime connection/response failed; input may already have been delivered. "
                                   "Do not blindly retry a state-changing action.") from exc
        self._terminal = result.terminal
        return result

    def stop(self) -> None:
        self._used = True
        if self._stopped:
            return
        if self._start_sent:
            try:
                # The host acknowledges ownership of grading/cleanup immediately.
                # Server shutdown must fit inside the controller's short stop grace.
                RuntimeClient(self.client.socket_path, timeout=5.0).call(
                    "close", session_id=self.session_id, owner_token=self._owner_token,
                )
            except (OSError, ValueError, RuntimeFault) as exc:
                # Leave retry possible if cleanup could not be acknowledged.
                raise EnvironmentError("Could not release runtime session " + self.session_id) from exc
        self._stopped = True
        self._started = False
