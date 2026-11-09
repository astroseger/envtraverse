from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import signal
from typing import Any

import requests

from client_storage import ensure_readonly_directory, read_client_config, seal_storage, write_readonly_file

CLIENT_DIR = Path(__file__).resolve().parent
# Replaced from the required configuration under the client lock before use.
OBSERVATIONS_DIR = Path("/observations")
ACTIONS_DIR = OBSERVATIONS_DIR / "actions"
CLIENT_STATE_PATH = CLIENT_DIR / ".client_session.json"
CLIENT_LOCK_PATH = CLIENT_DIR / ".client.lock"
ENVIRONMENT_DESCRIPTION_PATH = OBSERVATIONS_DIR / "environment_description.md"

# Allow an hour of backend work plus five minutes to return or clean up.
HTTP_CONNECT_TIMEOUT_SECONDS = 10.0
HTTP_READ_TIMEOUT_SECONDS = 65 * 60.0
HTTP_TIMEOUT = (HTTP_CONNECT_TIMEOUT_SECONDS, HTTP_READ_TIMEOUT_SECONDS)

# Internal server traffic must not inherit shell proxy or authentication settings.
# This is local to this client process; the agent's own proxy settings are intact.
HTTP_SESSION = requests.Session()
HTTP_SESSION.trust_env = False

CLIENT_STATE_KEYS = {
    "observations_path",
    "token",
    "step_index",
    "terminal",
}


@dataclass(frozen=True)
class SavedResponseArtifacts:
    action_path: Path | None
    step_paths: list[Path]


def ignore_interrupt_and_term_signals() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)


def path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def action_artifact_path(action_index: int) -> Path:
    return ACTIONS_DIR / f"action_{action_index:04d}.txt"


def save_action_artifact(payload: dict) -> Path | None:
    step_index = int(payload["step"]["step_index"])
    if step_index == 0 and "action" not in payload:
        return None
    if step_index <= 0:
        raise RuntimeError(f"Invalid action artifact step index: {step_index}")
    if "action" not in payload:
        raise RuntimeError(f"Server response for step {step_index} did not include action_{step_index - 1:04d}.")

    action = payload["action"]
    if not isinstance(action, dict):
        raise RuntimeError("Server response action must be a JSON object.")

    ensure_readonly_directory(ACTIONS_DIR)
    path = action_artifact_path(step_index - 1)
    write_readonly_file(path, (json.dumps(action, indent=2) + "\n").encode("utf-8"))
    return path


def save_step_artifacts(payload: dict) -> list[Path]:
    step = payload["step"]
    step_index = int(step["step_index"])
    prefix = f"step_{step_index:04d}"
    created_paths: list[Path] = []

    for artifact in step.get("artifacts", []):
        artifact_name = artifact["name"]
        if (
            not isinstance(artifact_name, str)
            or artifact_name in {"", ".", ".."}
            or any(character in artifact_name for character in "/\\\0")
        ):
            raise RuntimeError(f"Invalid artifact name from server: {artifact_name}")
        path = OBSERVATIONS_DIR / f"{prefix}_{artifact_name}"
        write_readonly_file(path, base64.b64decode(artifact["content_base64"], validate=True))
        created_paths.append(path)

    return created_paths


def save_response_artifacts(payload: dict) -> SavedResponseArtifacts:
    return SavedResponseArtifacts(
        action_path=save_action_artifact(payload),
        step_paths=save_step_artifacts(payload),
    )


def saved_artifacts_for_response(response: dict) -> list[Path]:
    step = response["step"]
    step_index = int(step["step_index"])
    prefix = f"step_{step_index:04d}"
    return [OBSERVATIONS_DIR / f"{prefix}_{artifact['name']}" for artifact in step.get("artifacts", [])]


def saved_artifacts_for_step(step_index: int) -> list[Path]:
    prefix = f"step_{step_index:04d}_"
    return sorted(path for path in OBSERVATIONS_DIR.iterdir() if path.is_file() and path.name.startswith(prefix))


def read_client_state() -> dict[str, Any]:
    if not CLIENT_STATE_PATH.is_file():
        raise FileNotFoundError("No active client session. Start a game first.")
    state = json.loads(CLIENT_STATE_PATH.read_text(encoding="utf-8"))
    state.setdefault("terminal", False)
    keys = set(state)
    if keys != CLIENT_STATE_KEYS:
        extra = sorted(keys - CLIENT_STATE_KEYS)
        missing = sorted(CLIENT_STATE_KEYS - keys)
        details = []
        if missing:
            details.append(f"missing keys: {', '.join(missing)}")
        if extra:
            details.append(f"unexpected keys: {', '.join(extra)}")
        raise RuntimeError(f"Invalid .client_session.json ({'; '.join(details)}).")

    if not str(state["token"]).strip():
        raise RuntimeError("Invalid .client_session.json: token is required.")
    expected_observations_path = str(OBSERVATIONS_DIR)
    actual_observations_path = str(state["observations_path"]).strip()
    if not actual_observations_path:
        raise RuntimeError("Invalid .client_session.json: observations_path is required.")
    if actual_observations_path != expected_observations_path:
        raise RuntimeError(
            ".client_session.json belongs to a different observations folder: "
            f"{actual_observations_path}; expected {expected_observations_path}."
        )
    if not OBSERVATIONS_DIR.is_dir():
        raise RuntimeError(f"Observations folder is missing: {OBSERVATIONS_DIR}")
    step_index = int(state["step_index"])
    if step_index < 0:
        raise RuntimeError("Invalid .client_session.json: step_index must be non-negative.")
    state["step_index"] = step_index
    state["terminal"] = bool(state["terminal"])
    return state


def write_client_state(state: dict[str, Any]) -> None:
    if set(state) != CLIENT_STATE_KEYS:
        raise RuntimeError("Refusing to write .client_session.json with unexpected keys.")
    write_readonly_file(CLIENT_STATE_PATH, json.dumps(state, indent=2).encode("utf-8"))


@contextmanager
def client_lock():
    global OBSERVATIONS_DIR, ACTIONS_DIR, ENVIRONMENT_DESCRIPTION_PATH
    configuration = read_client_config(CLIENT_DIR)
    # The launcher creates the lock before sealing /client. O_CREAT also allows
    # a fresh, writable standalone copy; no chmod happens before we own the lock.
    fd = os.open(CLIENT_LOCK_PATH, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, 0o444)
    with os.fdopen(fd, "rb") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            OBSERVATIONS_DIR = configuration.observations_path.resolve()
            ACTIONS_DIR = OBSERVATIONS_DIR / "actions"
            ENVIRONMENT_DESCRIPTION_PATH = OBSERVATIONS_DIR / "environment_description.md"
            seal_storage(CLIENT_DIR, OBSERVATIONS_DIR)
            yield configuration
        finally:
            try:
                seal_storage(CLIENT_DIR, OBSERVATIONS_DIR)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def print_step_summary(response: dict[str, Any], created_paths: list[Path]) -> None:
    print(f"step: {response['step']['step_index']}")
    print_artifact_paths(created_paths)


def print_move_summary(response: dict[str, Any], saved_artifacts: SavedResponseArtifacts) -> None:
    if saved_artifacts.action_path is not None:
        print(f"saved action: {saved_artifacts.action_path}")
    print(f"advanced to step: {response['step']['step_index']}")
    print_artifact_paths(saved_artifacts.step_paths)


def print_recovery_summary(response: dict[str, Any], saved_artifacts: SavedResponseArtifacts) -> None:
    if saved_artifacts.action_path is not None:
        print(f"recovered action: {saved_artifacts.action_path}")
    print(f"recovered step: {response['step']['step_index']}")
    print_artifact_paths(saved_artifacts.step_paths)


def print_artifact_paths(paths: list[Path]) -> None:
    grouped_paths: dict[Path, list[str]] = {}
    for path in paths:
        grouped_paths.setdefault(path.parent, []).append(path.name)
    for directory, file_names in grouped_paths.items():
        print(f"path: {directory}")
        print("files:")
        for file_name in file_names:
            print(f"  {file_name}")


def session_token_from_state(state: dict[str, Any], *, allow_missing: bool = False) -> str | None:
    token = state.get("token")
    if token is None or str(token).strip() == "":
        if allow_missing:
            return None
        raise RuntimeError("No token in .client_session.json. Start a game first.")
    return str(token).strip()


def parse_action_argument(action_text: str) -> dict[str, Any]:
    try:
        action = json.loads(action_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"action must be a JSON object: {exc.msg}") from None
    if not isinstance(action, dict):
        raise ValueError("action must be a JSON object")
    return action


def post_json(
    path: str, payload: dict[str, Any], session_token: str | None = None, *, server_url: str
) -> dict[str, Any]:
    headers: dict[str, str] = {}
    if session_token:
        headers["X-Session-Token"] = session_token
    response = HTTP_SESSION.post(f"{server_url}{path}", json=payload, headers=headers, timeout=HTTP_TIMEOUT)
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(f"Server returned a non-JSON response for {path}: HTTP {response.status_code}") from None
    if not response.ok:
        raise RuntimeError(data.get("error", f"Request failed: {response.status_code}"))
    return data


def get_json(path: str, session_token: str | None = None, *, server_url: str) -> dict[str, Any]:
    headers: dict[str, str] = {}
    if session_token:
        headers["X-Session-Token"] = session_token
    response = HTTP_SESSION.get(f"{server_url}{path}", headers=headers, timeout=HTTP_TIMEOUT)
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(f"Server returned a non-JSON response for {path}: HTTP {response.status_code}") from None
    if not response.ok:
        raise RuntimeError(data.get("error", f"Request failed: {response.status_code}"))
    return data


def save_environment_description(payload: dict[str, Any]) -> Path:
    description = payload.get("environment_description")
    if not isinstance(description, str) or not description.strip():
        raise RuntimeError("Server returned an invalid environment description")
    write_readonly_file(ENVIRONMENT_DESCRIPTION_PATH, description.encode("utf-8"))
    return ENVIRONMENT_DESCRIPTION_PATH


def repair_if_needed(
    state: dict[str, Any], *, server_url: str,
) -> tuple[dict[str, Any] | None, SavedResponseArtifacts | None]:
    response = get_json(
        "/game/last-step",
        session_token=session_token_from_state(state),
        server_url=server_url,
    )
    server_step_index = int(response["step"]["step_index"])

    if server_step_index < int(state["step_index"]):
        raise RuntimeError(
            f"Server is behind the client state: server step {server_step_index}, client step {state['step_index']}."
        )
    if server_step_index >= int(state["step_index"]) + 2:
        raise RuntimeError(
            f"Client state is too far behind to repair safely: server step {server_step_index}, "
            f"client step {state['step_index']}."
        )

    if server_step_index == int(state["step_index"]):
        server_terminal = bool(response["session"].get("terminal", False))
        if bool(state["terminal"]) != server_terminal:
            state["terminal"] = server_terminal
            write_client_state(state)
        return None, None

    saved_artifacts = save_response_artifacts(response)
    state["token"] = response["session"]["token"]
    state["step_index"] = server_step_index
    state["terminal"] = bool(response["session"].get("terminal", False))
    write_client_state(state)
    return response, saved_artifacts


def start_command(game_id: str) -> None:
    with client_lock() as configuration:
        if path_exists(CLIENT_STATE_PATH):
            raise FileExistsError(f".client_session.json already exists: {CLIENT_STATE_PATH}")
        if any(OBSERVATIONS_DIR.iterdir()):
            raise FileExistsError(f"Observations folder must be empty for a new game: {OBSERVATIONS_DIR}")
        response = post_json("/game/start", {"game_id": game_id}, server_url=configuration.server_url)
        description_path = save_environment_description(response)
        created_paths = save_step_artifacts(response)
        write_client_state(
            {
                "observations_path": str(OBSERVATIONS_DIR),
                "token": response["session"]["token"],
                "step_index": int(response["step"]["step_index"]),
                "terminal": bool(response["session"].get("terminal", False)),
            }
        )
    print(f"environment description: {description_path}")
    print_step_summary(response, created_paths)


def move_command(action_text: str) -> None:
    action = parse_action_argument(action_text)
    payload: dict[str, Any] = {"action": action}
    with client_lock() as configuration:
        state = read_client_state()
        repair_response, repaired_artifacts = repair_if_needed(state, server_url=configuration.server_url)
        if bool(state["terminal"]):
            raise RuntimeError("Session is terminal; refusing to send another action.")
        response = post_json(
            "/game/action",
            payload,
            session_token=session_token_from_state(state),
            server_url=configuration.server_url,
        )
        saved_artifacts = save_response_artifacts(response)
        state["token"] = response["session"]["token"]
        state["step_index"] = int(response["step"]["step_index"])
        state["terminal"] = bool(response["session"].get("terminal", False))
        write_client_state(state)

    if repair_response is not None and repaired_artifacts is not None:
        print(
            "The previous client run reached the server but did not commit local state. "
            "Recovered the last server step before making the new move."
        )
        print_recovery_summary(repair_response, repaired_artifacts)
    print_move_summary(response, saved_artifacts)


def status_command() -> None:
    with client_lock() as configuration:
        state = read_client_state()
        repair_response, repaired_artifacts = repair_if_needed(state, server_url=configuration.server_url)
        step_index = int(state["step_index"])
        paths = saved_artifacts_for_step(step_index)
    if repair_response is not None and repaired_artifacts is not None:
        print(
            "The previous client run reached the server but did not commit local state. "
            "Recovered the last server step before printing status."
        )
        print_recovery_summary(repair_response, repaired_artifacts)
    print(f"step: {step_index}")
    print_artifact_paths(paths)


def environment_description_command() -> None:
    with client_lock() as configuration:
        state = read_client_state()
        result = get_json(
            "/game/environment-description",
            session_token=session_token_from_state(state),
            server_url=configuration.server_url,
        )
        save_environment_description(result)
    print(json.dumps(result, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CLI client for the local server. One command per move."
    )
    subparsers = parser.add_subparsers(dest="command")

    start_parser = subparsers.add_parser("start", help="Start a new game session.")
    start_parser.add_argument("game_id")

    move_parser = subparsers.add_parser("move", help="Send one action to the active game session.")
    move_parser.add_argument("action")

    subparsers.add_parser("status", help="Print the local client step and saved artifacts.")
    subparsers.add_parser(
        "environment-description",
        help="Fetch and save the environment description for the active session.",
    )
    return parser


def main() -> int:
    ignore_interrupt_and_term_signals()
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "start":
            start_command(args.game_id)
        elif args.command == "move":
            move_command(args.action)
        elif args.command == "status":
            status_command()
        elif args.command == "environment-description":
            environment_description_command()
        else:
            parser.error("a command is required.")
        return 0
    except requests.exceptions.RequestException as exc:
        print(f"Server request failed: {exc}")
        print("Start the server first: python3 server/server.py --environment <name>")
        return 1
    except FileExistsError as exc:
        print(str(exc))
        return 1
    except FileNotFoundError as exc:
        print(str(exc))
        print("Start a game first: python3 /client/client.py start <game_id>")
        return 1
    except OSError as exc:
        print(f"Client storage error: {exc}")
        return 1
    except RuntimeError as exc:
        print(str(exc))
        return 1
    except ValueError as exc:
        print(f"Invalid command: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
