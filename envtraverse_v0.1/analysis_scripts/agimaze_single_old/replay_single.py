#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
import urllib.request


SCRIPT_DIR = Path(__file__).resolve().parent
SERVER_DIR = SCRIPT_DIR.parent / "src" / "server"
LABYRINTH_DIR = SERVER_DIR / "labyrinth"
ACTIONS_FILENAME = "actions.json"
RESULTS_FILENAME = "results.json"


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc


@dataclass(frozen=True)
class SavedLabyrinthReplay:
    actions_path: Path
    results_path: Path
    game_name: str
    labyrinth_index: int
    labyrinth_name: str
    actions: tuple[dict[str, Any], ...]


def actions_path_for_run_folder(run_folder: Path) -> Path:
    return run_folder / ACTIONS_FILENAME


def results_path_for_run_folder(run_folder: Path) -> Path:
    return run_folder / RESULTS_FILENAME


def _load_json_list(path: Path, description: str) -> list[Any]:
    if not path.is_file():
        raise FileNotFoundError(
            f"{description} does not exist: {path}. "
            f"Run analyse_runs.py on {path.parent} first."
        )

    payload = read_json(path)
    if not isinstance(payload, list):
        raise ValueError(f"Expected a list in {description.lower()} {path}")
    return payload


def _labyrinth_name_for_index(results_path: Path, labyrinth_index: int) -> str:
    payload = _load_json_list(results_path, "Results file")
    entries_by_index: dict[int, str] = {}

    for entry_position, entry in enumerate(payload):
        if not isinstance(entry, dict):
            raise ValueError(
                f"Result entry {entry_position} in {results_path} must be an object"
            )

        raw_index = entry.get("labyrinth_index")
        if (
            not isinstance(raw_index, int)
            or isinstance(raw_index, bool)
            or raw_index < 0
        ):
            raise ValueError(
                f"Result entry {entry_position} in {results_path} requires a "
                "non-negative integer labyrinth_index"
            )
        if raw_index in entries_by_index:
            raise ValueError(
                f"Duplicate labyrinth_index {raw_index} in {results_path}"
            )

        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f"Result entry {entry_position} in {results_path} requires a "
                "non-empty name"
            )
        entries_by_index[raw_index] = name.strip()

    try:
        return entries_by_index[labyrinth_index]
    except KeyError as exc:
        raise IndexError(
            f"labyrinth_index {labyrinth_index} was not found in {results_path}"
        ) from exc


def _actions_for_labyrinth(actions_path: Path, labyrinth_name: str) -> tuple[dict[str, Any], ...]:
    payload = _load_json_list(actions_path, "Actions file")
    matching_actions: tuple[dict[str, Any], ...] | None = None

    for entry_position, entry in enumerate(payload):
        if not isinstance(entry, dict):
            raise ValueError(
                f"Action entry {entry_position} in {actions_path} must be an object"
            )

        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f"Action entry {entry_position} in {actions_path} requires a "
                "non-empty name"
            )

        raw_actions = entry.get("actions")
        if not isinstance(raw_actions, list):
            raise ValueError(
                f"Action entry {entry_position} in {actions_path} requires an "
                "actions list"
            )

        actions: list[dict[str, Any]] = []
        for action_index, action in enumerate(raw_actions):
            if not isinstance(action, dict):
                raise ValueError(
                    f"Action {action_index} in entry {entry_position} of "
                    f"{actions_path} must be an object"
                )
            action_id(action)
            actions.append(action)

        if name.strip() == labyrinth_name:
            if matching_actions is not None:
                raise ValueError(
                    f"Duplicate labyrinth name {labyrinth_name!r} in {actions_path}"
                )
            matching_actions = tuple(actions)

    if matching_actions is None:
        raise ValueError(
            f"Labyrinth {labyrinth_name!r} from results file was not found in "
            f"{actions_path}"
        )
    return matching_actions


def load_saved_labyrinth(run_folder: Path, labyrinth_index: int) -> SavedLabyrinthReplay:
    if labyrinth_index < 0:
        raise ValueError("labyrinth_index must be non-negative")

    actions_path = actions_path_for_run_folder(run_folder)
    results_path = results_path_for_run_folder(run_folder)
    labyrinth_name = _labyrinth_name_for_index(results_path, labyrinth_index)
    actions = _actions_for_labyrinth(actions_path, labyrinth_name)

    return SavedLabyrinthReplay(
        actions_path=actions_path,
        results_path=results_path,
        game_name=run_folder.name,
        labyrinth_index=labyrinth_index,
        labyrinth_name=labyrinth_name,
        actions=actions,
    )


def http_get_json(url: str, timeout: float = 5.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected JSON object from {url}")
    return payload


def http_post_json(url: str, payload: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            loaded = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        loaded = json.loads(exc.read().decode("utf-8"))
    if not isinstance(loaded, dict):
        raise RuntimeError(f"Expected JSON object from {url}")
    return loaded


def action_id(action: dict[str, Any]) -> str:
    raw_action_id = action.get("id")
    if not isinstance(raw_action_id, str) or not raw_action_id.strip():
        raise ValueError(f"Action does not contain a string id: {action}")
    return raw_action_id.strip()


def format_json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def wait_until_ready(base_url: str, process: subprocess.Popen[bytes], timeout_s: float) -> None:
    deadline = time.time() + timeout_s
    last_error: Exception | None = None
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Labyrinth server exited early with code {process.returncode}")
        try:
            http_get_json(f"{base_url}/api/description?lang=en", timeout=1.0)
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.1)
    raise RuntimeError(f"Labyrinth server did not start at {base_url}: {last_error}")


def start_server(host: str, port: int, timeout_s: float) -> subprocess.Popen[bytes]:
    if not LABYRINTH_DIR.is_dir():
        raise FileNotFoundError(f"Labyrinth folder does not exist: {LABYRINTH_DIR}")

    process = subprocess.Popen(
        [
            "python3",
            "scripts/webui_server.py",
            "--host",
            host,
            "--port",
            str(port),
            "--debug",
        ],
        cwd=LABYRINTH_DIR,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    wait_until_ready(f"http://{host}:{port}", process, timeout_s)
    return process


def stop_server(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2.0)


def print_replay_state(label: str, response: dict[str, Any]) -> None:
    print(f"\n=== {label} ===")
    if response.get("error"):
        print(f"error: {format_json_compact(response)}")
        return

    formal = response.get("formal") if isinstance(response.get("formal"), dict) else {}
    status = response.get("status") or formal.get("status")
    observe = response.get("observe") or formal.get("observe")
    events = response.get("events") or formal.get("events")
    print(f"text: {response.get('text', '')}")
    if status is not None:
        print(f"status: {status}")
    if observe is not None:
        print(f"observe: {observe}")
    if events:
        print(f"events: {format_json_compact(events)}")
    print(f"inventory: {format_json_compact(response.get('inventory'))}")
    if "done" in response:
        print(f"done: {bool(response.get('done'))}")

    replay_map = response.get("map")
    if isinstance(replay_map, str) and replay_map:
        print("map:")
        print(replay_map.rstrip())


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replay one labyrinth selected by labyrinth_index from results.json "
            "using actions.json against the local "
            "labyrinth WebUI server and print status plus debug map after each move."
        )
    )
    parser.add_argument(
        "run_folder",
        type=Path,
        help="Run folder containing actions.json and results.json",
    )
    parser.add_argument(
        "labyrinth_index",
        type=int,
        help="Zero-based labyrinth index, matching the saved labyrinth_index field.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--lang", default="en")
    parser.add_argument("--timeout", type=float, default=10.0, help="Server startup timeout in seconds")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    run_folder = args.run_folder.expanduser().resolve()
    if not run_folder.is_dir():
        print(f"Run folder does not exist: {run_folder}", file=sys.stderr)
        return 1
    if args.labyrinth_index < 0:
        print("labyrinth_index must be non-negative", file=sys.stderr)
        return 1

    process: subprocess.Popen[bytes] | None = None
    try:
        saved = load_saved_labyrinth(run_folder, args.labyrinth_index)

        base_url = f"http://{args.host}:{args.port}"
        print(f"run_folder: {run_folder}")
        print(f"actions_file: {saved.actions_path}")
        print(f"results_file: {saved.results_path}")
        print(f"game_name: {saved.game_name}")
        print(f"labyrinth_index: {saved.labyrinth_index}")
        print(f"labyrinth_name: {saved.labyrinth_name}")
        print(f"actions: {len(saved.actions)}")
        print(f"server: {base_url}")

        process = start_server(args.host, args.port, args.timeout)
        start = http_post_json(
            f"{base_url}/api/start",
            {
                "path": saved.labyrinth_name,
                "lang": args.lang,
                "client": "api",
                "agent": "replay_single",
            },
        )
        if start.get("error"):
            raise RuntimeError(f"Failed to start labyrinth {saved.labyrinth_name!r}: {start}")
        session_id = start.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError(f"Labyrinth start response did not include a session id: {start}")

        print_replay_state("start", start)

        seq = 1
        for action_index, action in enumerate(saved.actions):
            move = action_id(action)
            out = http_post_json(
                f"{base_url}/api/step",
                {"id": session_id, "action": move, "seq": seq},
            )
            print_replay_state(f"action_{action_index:04d} seq={seq} action={move}", out)
            if out.get("error"):
                return 1
            if out.get("done"):
                break
            seq += 1

        return 0
    except (FileNotFoundError, ValueError, IndexError, RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if process is not None:
            stop_server(process)


if __name__ == "__main__":
    raise SystemExit(main())
