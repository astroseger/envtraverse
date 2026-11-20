"""EnvTraverse adapter; the external host runtime owns Minecraft and recordings."""
from __future__ import annotations

from dataclasses import fields
import math
import os
from typing import Any
from uuid import UUID, uuid4

from .actions import validate_action
from .catalog import get_task
from .compat import EnvironmentError, EnvironmentStartError, FatalEnvironmentError, InvalidActionError, Observation, StartResult
from .protocol import RuntimeClient, RuntimeFault, decode_observation
from .settings import Settings
from .resources import read_resource_context


def _session_key(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("session_id must be a UUID")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError("session_id must be a UUID") from exc
    if value.lower() not in (parsed.hex, str(parsed)):
        raise ValueError("session_id must use canonical UUID or hexadecimal form")
    return parsed.hex


def _observation(raw: dict[str, Any]) -> Observation:
    files, terminal = decode_observation(raw)
    return Observation(files=files, terminal=terminal)


class EnvironmentManager:
    def __init__(self, parameters: dict[str, Any] | None = None):
        parameters = {} if parameters is None else parameters
        setting_keys = {field.name for field in fields(Settings)}
        if not isinstance(parameters, dict) or set(parameters) - setting_keys - {"socket_path", "session_id", "request_timeout_seconds"}:
            raise ValueError("Unknown minecraft_realtime environment parameter")
        self.settings = Settings.from_dict({key: value for key, value in parameters.items() if key in setting_keys})
        path = parameters.get("socket_path", os.environ.get("MC_RUNTIME_SOCKET", "/run/minecraft-realtime/runtime.sock"))
        if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
            raise ValueError("socket_path must be an absolute pathname")
        timeout = parameters.get("request_timeout_seconds", self.settings.startup_timeout_seconds + 60)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("request_timeout_seconds must be a positive finite number")
        self.session_id = _session_key(parameters.get("session_id", uuid4().hex))
        self.resources = read_resource_context(session_id=self.session_id)
        self._owner_token = uuid4().hex
        self.client = RuntimeClient(path, timeout=timeout)
        self._used = self._started = self._start_sent = self._stopped = self._terminal = False

    def _call(self, method: str, **parameters: Any) -> dict[str, Any]:
        return self.client.call(method, session_id=self.session_id, owner_token=self._owner_token, **parameters)

    def start_environment(self, game_id: str) -> StartResult:
        if self._used:
            raise EnvironmentStartError("This environment has already been started or stopped")
        self._used = True
        try:
            task = get_task(game_id)
            if task["requires_hostiles"] and self.settings.difficulty == "peaceful":
                raise RuntimeFault("invalid_settings", "This task requires hostile mobs; choose easy, normal or hard")
            self._start_sent = True
            resources = {"resources": self.resources} if self.resources else {}
            raw = self._call("start", game_id=game_id, settings=self.settings.to_dict(), **resources)
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
                self._start_sent = False
            try:
                self.stop()
            except EnvironmentError:
                pass
            message = str(exc) if isinstance(exc, RuntimeFault) else "Minecraft task preparation failed"
            raise EnvironmentStartError(message) from exc

    def move(self, action: dict[str, Any]) -> Observation:
        if not self._started or self._stopped:
            raise EnvironmentError("Environment is not running")
        if self._terminal:
            raise InvalidActionError("The environment is terminal")
        try:
            action = validate_action(action, width=self.settings.width, height=self.settings.height)
            raw = self._call("action", action=action)
            result = _observation(raw["observation"])
        except RuntimeFault as exc:
            if exc.code in {"invalid_action", "bad_request", "terminal"}:
                raise InvalidActionError(str(exc)) from exc
            if exc.code in {"worker_failed", "unknown_session", "not_ready", "unavailable", "not_owner"}:
                raise FatalEnvironmentError(str(exc)) from exc
            raise EnvironmentError(str(exc)) from exc
        except (OSError, ValueError, KeyError) as exc:
            raise FatalEnvironmentError("Runtime connection failed; input may already have been delivered. Do not blindly retry it.") from exc
        self._terminal = result.terminal
        return result

    def stop(self) -> None:
        self._used = True
        if self._stopped:
            return
        if self._start_sent:
            try:
                RuntimeClient(self.client.socket_path, timeout=5).call("close", session_id=self.session_id, owner_token=self._owner_token)
            except (OSError, ValueError, RuntimeFault) as exc:
                raise EnvironmentError("Could not release Minecraft runtime session") from exc
        self._stopped = True
        self._started = False
