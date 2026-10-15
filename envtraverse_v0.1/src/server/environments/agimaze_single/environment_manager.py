"""Single-labyrinth environment manager."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError
import urllib.request

from environment_api import (
    ENVIRONMENT_OPERATION_TIMEOUT_SECONDS,
    EnvironmentError,
    EnvironmentStartError,
    InvalidActionError,
    Observation,
    StartResult,
)

LOGGER = logging.getLogger("env-server.agimaze_single")
LABYRINTH_DIR = Path(__file__).resolve().parent / "labyrinth"
DEFAULT_PORT = 5000
BACKEND_START_TIMEOUT_SECONDS = 10.0
READINESS_PROBE_TIMEOUT_SECONDS = 1.0
DEFAULT_LANGUAGE = "en"
DEFAULT_MAX_STEPS = 200
MAX_STEPS_MULTIPLIER = 5


def http_get_json(
    url: str, *, timeout: float = ENVIRONMENT_OPERATION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected JSON object from {url}")
    return payload


def http_post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=ENVIRONMENT_OPERATION_TIMEOUT_SECONDS) as response:
            loaded = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        loaded = json.loads(exc.read().decode("utf-8"))
    if not isinstance(loaded, dict):
        raise RuntimeError(f"Expected JSON object from {url}")
    return loaded


def make_prompt(description: str, start: dict[str, Any]) -> str:
    task_meta = start.get("task_meta") or {}
    info = start.get("info")
    public_map = start.get("map", "")
    actions = start.get("actions") or ["up", "down", "left", "right"]

    info_str = ""
    if isinstance(info, dict):
        info_str = (
            f"info: rivers={info.get('rivers')} "
            f"pits={info.get('pits_cycles')}x{info.get('pits_cycle_len')}"
        )

    board_str = ""
    board = task_meta.get("board") if isinstance(task_meta, dict) else None
    if isinstance(board, dict):
        board_str = f"board size: n={board.get('n')} rows, m={board.get('m')} cols"

    start_pos_str = ""
    start_pos = task_meta.get("start") if isinstance(task_meta, dict) else None
    if isinstance(start_pos, dict):
        start_pos_str = f"start position (row,col): ({start_pos.get('row')},{start_pos.get('col')})"

    action_examples = ", ".join(
        json.dumps({"id": str(action)}, ensure_ascii=False)
        for action in actions
    )
    return (
        "You are playing a grid labyrinth game. Your goal is to win the game by collecting the treasure and exiting.\n\n"
        "Rules/description:\n" + description + "\n\n"
        "Task info:\n" + info_str + "\n" + board_str + "\n" + start_pos_str + "\n\n"
        "Map (public):\n" + str(public_map) + "\n\n"
        "Actions are JSON objects. Available actions: " + action_examples + "\n\n"
        "Coordinate convention: positions are given as (row, col), where row increases downward and col increases to the right.\n\n"
        "You will receive the result of each move as text.\n"
        "Choose one action each step. Avoid wasting moves."
    )


@dataclass(frozen=True)
class EnvironmentManagerParameters:
    port: int = DEFAULT_PORT
    lang: str = DEFAULT_LANGUAGE
    seed: int | None = None

    @classmethod
    def from_dict(cls, raw_parameters: dict[str, Any] | None) -> "EnvironmentManagerParameters":
        raw_parameters = raw_parameters or {}
        unknown = raw_parameters.keys() - {"port", "lang", "seed"}
        if unknown:
            raise ValueError(f"Unknown environment parameters: {', '.join(sorted(unknown))}")
        port = int(raw_parameters.get("port", DEFAULT_PORT))
        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        lang = str(raw_parameters.get("lang", DEFAULT_LANGUAGE)).strip() or DEFAULT_LANGUAGE
        raw_seed = raw_parameters.get("seed")
        seed = int(raw_seed) if raw_seed is not None else None
        return cls(port=port, lang=lang, seed=seed)


def state_files(state: dict[str, Any]) -> dict[str, str | bytes]:
    content = json.dumps(
        {
            "text": str(state.get("text", "")),
            "inventory": state.get("inventory"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return {"state.txt": content + "\n"}


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2.0)


class EnvironmentManager:
    def __init__(self, parameters: dict[str, Any] | None = None) -> None:
        self.parameters = EnvironmentManagerParameters.from_dict(parameters)
        if not LABYRINTH_DIR.is_dir():
            raise FileNotFoundError(f"Labyrinth folder does not exist: {LABYRINTH_DIR}")
        self.port = self.parameters.port
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.process: subprocess.Popen[bytes] | None = None
        self.session_id = ""
        self.actions: list[str] = []
        self.max_steps = DEFAULT_MAX_STEPS
        self.step = 1
        self.terminal = False
        self.last_state: dict[str, Any] | None = None
        self._used = False

    def start_environment(self, game_id: str) -> StartResult:
        if self._used:
            raise EnvironmentStartError("This environment has already been started or stopped")
        self._used = True
        game_path = str(game_id).strip()
        if not game_path:
            raise EnvironmentStartError("game_id is required")
        try:
            self.process = subprocess.Popen(
                [sys.executable, "scripts/webui_server.py", "--host", "127.0.0.1", "--port", str(self.port)],
                cwd=LABYRINTH_DIR,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._wait_until_ready(self.base_url, self.process)
            description = http_get_json(f"{self.base_url}/api/description?lang={self.parameters.lang}").get("description", "")
            start = http_post_json(
                f"{self.base_url}/api/start",
                {
                    "path": game_path,
                    "lang": self.parameters.lang,
                    "seed": self.parameters.seed,
                    "client": "api",
                    "agent": "environment_manager",
                },
            )
            if start.get("error"):
                raise EnvironmentStartError("Failed to start labyrinth game")
            session_id = start.get("id")
            if not isinstance(session_id, str) or not session_id:
                raise EnvironmentStartError("Labyrinth start response did not include a session id")

            task_meta = start.get("task_meta") or {}
            raw_max_steps = task_meta.get("max_steps") if isinstance(task_meta, dict) else None
            max_steps = raw_max_steps if isinstance(raw_max_steps, int) and raw_max_steps > 0 else DEFAULT_MAX_STEPS
            self.session_id = session_id
            self.max_steps = max_steps * MAX_STEPS_MULTIPLIER
            self.actions = [str(action) for action in (start.get("actions") or ["up", "down", "left", "right"])]
            self.last_state = start
            result = StartResult(
                environment_description=make_prompt(str(description), start),
                observation=Observation(files=state_files(start), terminal=self.terminal),
            )
            LOGGER.info("started labyrinth game=%s session=%s port=%s", game_path, session_id, self.port)
            return result
        except BaseException:
            self.stop()
            raise

    def move(self, action: dict[str, Any]) -> Observation:
        if self.process is None:
            raise EnvironmentError("Environment game is closed")
        if self.terminal:
            raise InvalidActionError("Invalid action: environment is terminal")

        action_id = self._action_id(action)
        if action_id not in self.actions:
            raise InvalidActionError(
                f"Invalid action: unknown action id {action_id}; available actions: {', '.join(self.actions)}"
            )

        out = http_post_json(
            f"{self.base_url}/api/step",
            {"id": self.session_id, "action": action_id, "seq": self.step},
        )
        if out.get("error"):
            raise EnvironmentError(f"Labyrinth step failed: {out['error']}")

        self.step += 1
        self.terminal = bool(out.get("done")) or self.step > self.max_steps
        self.last_state = out
        return Observation(files=state_files(out), terminal=self.terminal)

    def state(self) -> dict[str, Any]:
        return dict(self.last_state or {})

    def _action_id(self, raw_action: dict[str, Any]) -> str:
        if not isinstance(raw_action, dict):
            raise InvalidActionError("Invalid action: action must be a JSON object")
        action_id = raw_action.get("id")
        if not isinstance(action_id, str) or not action_id.strip():
            raise InvalidActionError("Invalid action: action requires string id")
        return action_id.strip()

    def stop(self) -> None:
        self._used = True
        if self.process is not None:
            _terminate_process(self.process)
            self.process = None

    def _wait_until_ready(
        self,
        base_url: str,
        process: subprocess.Popen[bytes],
        timeout_s: float = BACKEND_START_TIMEOUT_SECONDS,
    ) -> None:
        deadline = time.monotonic() + timeout_s
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Labyrinth server exited early with code {process.returncode}")
            try:
                http_get_json(
                    f"{base_url}/api/description?lang={self.parameters.lang}",
                    timeout=READINESS_PROBE_TIMEOUT_SECONDS,
                )
                return
            except Exception as exc:
                last_error = exc
                time.sleep(0.1)
        raise RuntimeError(f"Labyrinth server did not start at {base_url}: {last_error}")
