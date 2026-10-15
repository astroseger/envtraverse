#!/usr/bin/env python3
"""Replay a maze selected by name from a JSON list of action records."""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import dataclass
import json
import math
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any, Iterator
from urllib.error import HTTPError
import urllib.request


LABYRINTH_DIR = Path(__file__).resolve().parents[2] / "src/server/environments/agimaze_single/labyrinth"


@dataclass(frozen=True)
class SavedLabyrinthReplay:
    json_path: Path
    name_key: str
    labyrinth_name: str
    actions: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class ReplayStep:
    index: int
    action: str | None
    response: dict[str, Any]

    @property
    def label(self) -> str:
        return "start" if self.index == 0 else f"action_{self.index - 1:04d}"


def action_id(action: dict[str, Any]) -> str:
    value = action.get("id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Action requires a non-empty string id")
    return value.strip()


def load_saved_labyrinth(json_path: Path, labyrinth_name: str) -> SavedLabyrinthReplay:
    json_path = json_path.expanduser().resolve()
    if not labyrinth_name.strip():
        raise ValueError("Maze name must not be empty")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{json_path}: expected a list of dictionaries")

    matches: list[tuple[int, str]] = []
    for index, record in enumerate(payload):
        if not isinstance(record, dict):
            raise ValueError(f"{json_path}: record {index + 1} must be a dictionary")
        matches.extend(
            (index, key) for key, value in record.items()
            if isinstance(value, str) and value == labyrinth_name
        )

    if not matches:
        raise ValueError(f"Maze {labyrinth_name!r} was not found in {json_path}")
    keys = sorted({key for _, key in matches})
    if len(keys) != 1:
        raise ValueError(f"{json_path}: ambiguous maze name {labyrinth_name!r}; matches keys {keys!r}")
    if len(matches) != 1:
        records = ", ".join(str(index + 1) for index, _ in matches)
        raise ValueError(
            f"{json_path}: duplicate maze name {labyrinth_name!r} under key {keys[0]!r} "
            f"in records {records}"
        )

    index, name_key = matches[0]
    actions = payload[index].get("actions")
    location = f"{json_path}: record {index + 1}"
    if not isinstance(actions, list):
        raise ValueError(f"{location} requires an actions list")
    for action_index, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            raise ValueError(f"{location}, action {action_index} must be a dictionary")
        try:
            action_id(action)
        except ValueError as exc:
            raise ValueError(f"{location}, action {action_index}: {exc}") from exc
    return SavedLabyrinthReplay(json_path, name_key, labyrinth_name, tuple(actions))


def validate_maze_path(labyrinth_name: str) -> None:
    relative_path = Path(labyrinth_name)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("Maze name must be a relative file path within the bundled packs directory")
    packs = (LABYRINTH_DIR / "packs").resolve()
    maze_path = (packs / relative_path).resolve()
    if not maze_path.is_relative_to(packs):
        raise ValueError(f"Maze path escapes packs directory: {labyrinth_name!r}")
    if not maze_path.is_file() or maze_path.suffix not in {".json", ".cfg"}:
        raise ValueError(f"Expected an exact maze .json or .cfg file, not a directory: {maze_path}")


def http_get_json(url: str, timeout: float = 5.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected JSON object from {url}")
    return payload


def http_post_json(url: str, payload: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            loaded = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        with exc:
            loaded = json.loads(exc.read().decode("utf-8"))
    if not isinstance(loaded, dict):
        raise RuntimeError(f"Expected JSON object from {url}")
    return loaded


def wait_until_ready(base_url: str, process: subprocess.Popen[bytes], timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Labyrinth server exited early with code {process.returncode}")
        try:
            http_get_json(f"{base_url}/api/description?lang=en", timeout=1.0)
        except (OSError, ValueError, RuntimeError) as exc:
            last_error = exc
        else:
            if process.poll() is not None:
                raise RuntimeError(f"Labyrinth server exited early with code {process.returncode}")
            return
        time.sleep(0.1)
    raise RuntimeError(f"Labyrinth server did not start at {base_url}: {last_error}")


def stop_server(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2.0)


def start_server(host: str, port: int, timeout_s: float) -> subprocess.Popen[bytes]:
    if not LABYRINTH_DIR.is_dir():
        raise FileNotFoundError(f"Labyrinth folder does not exist: {LABYRINTH_DIR}")
    # Refuse an occupied port rather than mistaking another server for our child.
    with socket.create_server((host, port)):
        pass
    process = subprocess.Popen(
        [sys.executable, "-B", "scripts/webui_server.py", "--host", host, "--port", str(port), "--debug"],
        cwd=LABYRINTH_DIR, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_until_ready(f"http://{host}:{port}", process, timeout_s)
    except BaseException:
        stop_server(process)
        raise
    return process


def replay_steps(
    saved: SavedLabyrinthReplay, *, host: str, port: int, lang: str, timeout: float,
) -> Iterator[ReplayStep]:
    """Yield initial state and each action result; callers close on early exit."""
    validate_maze_path(saved.labyrinth_name)
    base_url = f"http://{host}:{port}"
    process = start_server(host, port, timeout)
    try:
        start = http_post_json(f"{base_url}/api/start", {
            "path": saved.labyrinth_name, "lang": lang, "client": "api", "agent": "replay_single",
        })
        if start.get("error"):
            raise RuntimeError(f"Failed to start maze {saved.labyrinth_name!r}: {start}")
        session_id = start.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError(f"Labyrinth start response did not include a session id: {start}")
        yield ReplayStep(0, None, start)

        previous = start
        for seq, payload in enumerate(saved.actions, start=1):
            if previous.get("done"):
                raise RuntimeError(
                    f"Maze ended after {seq - 1} actions, but the input contains {len(saved.actions)} actions"
                )
            move = action_id(payload)
            response = http_post_json(f"{base_url}/api/step", {"id": session_id, "action": move, "seq": seq})
            if response.get("error"):
                raise RuntimeError(f"Replay failed at action_{seq - 1:04d} ({move}): {response}")
            yield ReplayStep(seq, move, response)
            previous = response
    finally:
        stop_server(process)


def format_json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def response_field(response: dict[str, Any], name: str) -> Any:
    value = response.get(name)
    if value is not None:
        return value
    formal = response.get("formal")
    return formal.get(name) if isinstance(formal, dict) else None


def print_replay_state(step: ReplayStep) -> None:
    label = step.label if step.action is None else f"{step.label} seq={step.index} action={step.action}"
    response = step.response
    print(f"\n=== {label} ===")
    print(f"text: {response.get('text', '')}")
    for name in ("status", "observe"):
        value = response_field(response, name)
        if value is not None:
            print(f"{name}: {value}")
    events = response_field(response, "events")
    if events:
        print(f"events: {format_json_compact(events)}")
    print(f"inventory: {format_json_compact(response.get('inventory'))}")
    if "done" in response:
        print(f"done: {bool(response['done'])}")
    replay_map = response.get("map")
    if isinstance(replay_map, str) and replay_map:
        print("map:")
        print(replay_map.rstrip())


def positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number of seconds")
    return seconds


def port_number(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def add_replay_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("json_file", type=Path, help="JSON file containing a list of dictionaries with actions")
    parser.add_argument("maze_name", help="Exact maze name, relative to the bundled packs directory")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=port_number, default=8000)
    parser.add_argument("--lang", default="en")
    parser.add_argument("--timeout", type=positive_seconds, default=10.0, help="Server startup timeout in seconds")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    add_replay_arguments(parser)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        saved = load_saved_labyrinth(args.json_file, args.maze_name)
        print(f"json_file: {saved.json_path}")
        print(f"name_key: {saved.name_key}")
        print(f"labyrinth_name: {saved.labyrinth_name}")
        print(f"actions: {len(saved.actions)}")
        print(f"server: http://{args.host}:{args.port}")
        with closing(replay_steps(saved, host=args.host, port=args.port, lang=args.lang, timeout=args.timeout)) as steps:
            for step in steps:
                print_replay_state(step)
    except KeyboardInterrupt:
        return 130
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
