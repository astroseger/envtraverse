"""Exercise a running runtime through the actual EnvironmentManager API.

Default: solve the supplied synthetic smoke-test-smoke-test, not an official benchmark task.
--probe-only: inspect a real task then submit WITHOUT claiming to solve it.
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .environment_manager import EnvironmentManager
from .catalog import SMOKE_GAME


def _send(game, text: str, tid: str = "t1") -> None:
    game.move({"id": "terminal_send", "terminal_id": tid,
               "keys": [{"text": text}, {"key": "Enter"}]})


def _wait_for(game, marker: str, tid: str = "t1") -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        obs = game.move({"id": "terminal_read", "terminal_id": tid, "wait_ms": 100})
        if obs.terminal:
            raise RuntimeError(f"Trial ended unexpectedly: {obs.files}")
        if marker in obs.files["screen.txt"]:
            return
    raise RuntimeError(f"Terminal output marker {marker!r} did not appear")


def run_smoke(socket_path: str | Path, task_id: str = SMOKE_GAME, probe_only: bool = False, session_id: str | None = None, output_dir: str | Path | None = None) -> dict:
    parameters = {"socket_path": str(Path(socket_path).absolute())}
    if session_id is not None:
        parameters["session_id"] = session_id
    manager = EnvironmentManager(parameters)
    print("SESSION_ID:", manager.session_id, flush=True)
    try:
        start = manager.start_environment(task_id)
        game = manager
        if "--- BEGIN TASK ---" not in start.environment_description:
            raise AssertionError("Task instruction was not included")
        initial = json.loads(start.observation.files["terminal.json"])
        if initial["terminal_id"] != "t1":
            raise AssertionError("Expected initial terminal t1")
        print("STARTED:", task_id, initial["columns"], "x", initial["rows"])
        _send(game, "pwd; printf 'TB_%s\\n' FIRST_READY")
        _wait_for(game, "TB_FIRST_READY")
        second = game.move({"id": "terminal_new"})
        tid2 = json.loads(second.files["terminal.json"])["terminal_id"]
        _send(game, "printf 'TB_%s\\n' SECOND_READY", tid2)
        _wait_for(game, "TB_SECOND_READY", tid2)
        # Exercise a real control key, not the literal characters C-c.
        _send(game, "sleep 60", tid2)
        game.move({"id": "terminal_read", "terminal_id": tid2, "wait_ms": 100})
        game.move({"id": "terminal_send", "terminal_id": tid2, "keys": [{"key": "C-c"}]})
        _send(game, "printf 'TB_%s\\n' INTERRUPTED", tid2)
        _wait_for(game, "TB_INTERRUPTED", tid2)
        if not probe_only:
            _send(game, "printf 'hello from terminal bench\\n' > /app/answer.txt; printf 'TB_%s\\n' FILE_READY")
            _wait_for(game, "TB_FILE_READY")
            fetched = game.move({"id": "get_file", "path": "/app/answer.txt"})
            if fetched.files["file.bin"] != b"hello from terminal bench\n":
                raise AssertionError("Retrieved file differs from expected bytes")
            print("FILE ROUND-TRIP: OK")
        final = game.move({"id": "finish"})
        if not final.terminal:
            raise AssertionError("finish did not terminate the environment")
        if "result.json" in final.files:
            raise AssertionError("Agent received verifier results")
        manager.stop()
        if output_dir is None:
            return {"session_id": manager.session_id, "submitted": True}
        result_path = Path(output_dir) / manager.session_id / "result.json"
        deadline = time.monotonic() + 180
        while not result_path.is_file():
            if time.monotonic() >= deadline:
                raise TimeoutError("Synthetic smoke validation did not finish")
            time.sleep(0.05)
        result = json.loads(result_path.read_text())
        print("FINAL:", json.dumps(result, indent=2))
        if result.get("status") != "finished" or not result.get("rewards"):
            raise AssertionError("Evaluation failed (this is not merely reward zero): " + json.dumps(result))
        if not probe_only and result["rewards"].get("reward") != 1.0:
            raise AssertionError("Synthetic smoke task did not receive reward 1")
        manager.stop()
        manager.stop()  # Idempotence.
        return result
    finally:
        manager.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--task", default=SMOKE_GAME)
    parser.add_argument("--output", type=Path, help="Host results directory; wait here for smoke verification")
    parser.add_argument("--session-id", help="Optional caller-selected UUID")
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--parallel", type=int, default=1, help="Independent managers through one runtime")
    args = parser.parse_args()
    if args.task != SMOKE_GAME and not args.probe_only:
        parser.error("Use --probe-only for tasks other than the supplied synthetic smoke-test-smoke-test")
    if not 1 <= args.parallel <= 32:
        parser.error("parallel must be between 1 and 32")
    if args.parallel > 1 and args.session_id is not None:
        parser.error("A fixed session ID cannot be shared by parallel trials")
    if args.parallel == 1:
        run_smoke(args.socket, args.task, args.probe_only, args.session_id, args.output)
    else:
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            futures = [pool.submit(run_smoke, args.socket, args.task, args.probe_only, None, args.output) for _ in range(args.parallel)]
            results = [future.result() for future in futures]
        print("PARALLEL SESSIONS:", json.dumps([r["session_id"] for r in results]))


if __name__ == "__main__":
    main()
