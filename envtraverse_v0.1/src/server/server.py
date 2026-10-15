"""Synchronous HTTP server for exactly one game run."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import signal
import sys
from typing import Any
from uuid import uuid4

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException
from werkzeug.serving import make_server

from artifacts import serialize_observation
from environment_api import (
    EnvironmentError,
    EnvironmentManager,
    EnvironmentStartError,
    FatalEnvironmentError,
    InvalidActionError,
    Observation,
    StartResult,
)
from environment_loader import available_environments, load_environment_manager

SERVER_HOST = os.environ.get("ENV_SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("ENV_SERVER_PORT", "8879"))
SERVER_LOG_PATH = Path(os.environ.get("ENV_SERVER_LOG_PATH", "server.log"))
LOGGER = logging.getLogger("env-server.server")
INTERNAL_ERROR_MESSAGE = "Environment request failed"


def setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    file_handler = logging.FileHandler(log_path, mode="x", encoding="utf-8")
    stream_handler = logging.StreamHandler(sys.stderr)
    for handler in (file_handler, stream_handler):
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler], force=True)


class RequestError(ValueError):
    """A client request that must not change the environment."""


def step_payload(step_index: int, observation: Observation) -> dict[str, Any]:
    return {"step_index": step_index, "artifacts": serialize_observation(observation)}


class GameServer:
    """One manager, one start attempt, and one cached session."""

    def __init__(self, manager: EnvironmentManager, *, game_id: str, game_alias: str) -> None:
        self.manager = manager
        self.game_id = game_id
        self.game_alias = game_alias
        self.start_attempted = False
        self.token: str | None = None
        self.description = ""
        self.step_index = 0
        self.terminal = False
        self.last_step: dict[str, Any] | None = None
        self.failed = False
        self.stopped = False

    def start(self, game_alias: Any) -> dict[str, Any]:
        if self.start_attempted or game_alias != self.game_alias:
            raise RequestError("unknown or already used game_id")
        self.start_attempted = True
        LOGGER.info("starting requested_game_id=%s game_id=%s", self.game_alias, self.game_id)
        try:
            result = self.manager.start_environment(self.game_id)
            if not isinstance(result, StartResult):
                raise TypeError("Environment manager must return a StartResult")
            if not isinstance(result.environment_description, str) or not result.environment_description.strip():
                raise ValueError("Environment manager must provide a nonempty environment_description")
            initial_step = step_payload(0, result.observation)
            self.description = result.environment_description
            self.token = uuid4().hex
            self.terminal = result.observation.terminal
            self.last_step = {
                "session": self.session_payload(),
                "environment_description": self.description,
                "step": initial_step,
            }
        except BaseException:
            self.failed = True
            self.stop()
            raise
        LOGGER.info(
            "started session token=%s requested_game_id=%s game_id=%s",
            self.token, self.game_alias, self.game_id,
        )
        return deepcopy(self.last_step)

    def require_token(self, token: str | None) -> None:
        if not token or self.token is None or token != self.token:
            raise RequestError("A valid X-Session-Token is required. Start a session first.")

    def session_payload(self) -> dict[str, Any]:
        return {"token": self.token, "terminal": self.terminal}

    def move(self, action: Any) -> dict[str, Any]:
        if self.terminal:
            raise InvalidActionError("Invalid action: environment is terminal")
        if not isinstance(action, dict):
            raise InvalidActionError("Invalid action: action must be a JSON object")
        observation = self.manager.move(action)
        step = step_payload(self.step_index + 1, observation)
        self.step_index += 1
        self.terminal = observation.terminal
        self.last_step = {
            "session": self.session_payload(),
            "action": deepcopy(action),
            "step": step,
        }
        LOGGER.info(
            "applied action session=%s action=%s step_index=%s",
            self.token, action, self.step_index,
        )
        return deepcopy(self.last_step)

    def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        try:
            self.manager.stop()
        except Exception:
            self.failed = True
            LOGGER.exception("failed to stop environment manager")


def create_app(game: GameServer) -> Flask:
    app = Flask(__name__)
    app.logger.handlers.clear()
    app.logger.propagate = True
    server_started = datetime.now(timezone.utc).isoformat()

    @app.before_request
    def log_request() -> Any:
        LOGGER.info("%s %s from %s", request.method, request.path, request.remote_addr)
        if game.failed or game.stopped:
            return jsonify({"error": "Server is shutting down."}), 503
        return None

    @app.after_request
    def log_response(response: Any) -> Any:
        if response.status_code >= 400:
            LOGGER.warning("%s %s -> %s", request.method, request.path, response.status)
        return response

    @app.errorhandler(Exception)
    def handle_error(exc: Exception) -> Any:
        if isinstance(exc, HTTPException):
            return exc
        if isinstance(exc, (RequestError, InvalidActionError, EnvironmentStartError)):
            LOGGER.warning("request rejected: %s", exc, exc_info=isinstance(exc, EnvironmentStartError))
            return jsonify({"error": str(exc)}), 400
        if isinstance(exc, EnvironmentError):
            if isinstance(exc, FatalEnvironmentError):
                game.failed = True
            LOGGER.exception("environment operation failed")
            return jsonify({"error": str(exc)}), 503
        LOGGER.exception("Unhandled exception while handling %s %s", request.method, request.path)
        return jsonify({"error": INTERNAL_ERROR_MESSAGE}), 500

    def json_body() -> dict[str, Any]:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise RequestError("Request body must be a JSON object")
        return payload

    def authenticate() -> None:
        game.require_token(request.headers.get("X-Session-Token"))

    @app.get("/health")
    def health() -> Any:
        return jsonify({"status": "ok", "server_started": server_started})

    @app.post("/game/start")
    def start_game() -> Any:
        return jsonify(game.start(json_body().get("game_id")))

    @app.post("/game/action")
    def apply_action() -> Any:
        authenticate()
        return jsonify(game.move(json_body().get("action")))

    @app.get("/game/last-step")
    def last_step() -> Any:
        authenticate()
        return jsonify(game.last_step)

    @app.get("/game/environment-description")
    def environment_description() -> Any:
        authenticate()
        return jsonify({"environment_description": game.description})

    return app


def nonempty(value: str) -> str:
    value = value.strip()
    if not value:
        raise argparse.ArgumentTypeError("must not be empty")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve one game run.")
    parser.add_argument("--environment", required=True, choices=available_environments())
    parser.add_argument("--game-id", required=True, type=nonempty, help="Private environment-specific game ID.")
    parser.add_argument("--game-alias", required=True, type=nonempty, help="Public alias accepted by /game/start.")
    parser.add_argument("--host", default=SERVER_HOST)
    parser.add_argument("--port", type=int, default=SERVER_PORT)
    parser.add_argument("--parameters", default="{}", help="Environment parameters as a JSON object.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        parameters = json.loads(args.parameters)
        if not isinstance(parameters, dict):
            raise ValueError("--parameters must be a JSON object")
        manager_class = load_environment_manager(args.environment)
        setup_logging(SERVER_LOG_PATH)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1

    def interrupt(signum: int, _frame: Any) -> None:
        LOGGER.info("signal %s received", signum)
        raise SystemExit(128 + signum)

    previous_handlers = {
        signum: signal.signal(signum, interrupt)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    game: GameServer | None = None
    try:
        manager = manager_class(parameters)
        game = GameServer(manager, game_id=args.game_id, game_alias=args.game_alias)
        app = create_app(game)
        # All backend calls and cleanup happen on this thread. A fatal response
        # finishes sending before the loop exits and closes the environment.
        with make_server(args.host, args.port, app, threaded=False) as http:
            LOGGER.info(
                "starting server environment=%s host=%s port=%s log=%s",
                args.environment, args.host, http.server_port, SERVER_LOG_PATH,
            )
            while not game.failed:
                http.handle_request()
    except Exception:
        LOGGER.exception("server failed")
        return 1
    finally:
        if game is not None:
            game.stop()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return 1 if game.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
