"""Live timeout/frames/controls check through the unchanged EnvTraverse HTTP server.

Requires built game images, Flask (the ordinary server dependency), Docker, and
EULA acceptance. Creates a disposable fresh world, never uses model credentials,
and retains its report/recordings in the specified new run root.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--accept-eula", action="store_true")
    args = parser.parse_args(argv)
    if not args.accept_eula and os.getenv("EULA", "").lower() != "true":
        parser.error("Use --accept-eula after accepting the Minecraft EULA")
    root = args.run_root.resolve()
    if (root / "run").exists() or (root / "run_mc").exists():
        parser.error("Choose a fresh run root without run/ or run_mc/")
    package = Path(__file__).resolve().parent
    server_source = package.parents[1] / "server.py"
    if not server_source.is_file():
        parser.error("Run this smoke test from the installed EnvTraverse environment package")
    root.mkdir(parents=True, exist_ok=True)
    alias = uuid4().hex
    workspace = root / "run" / alias / "run/agent_run"
    workspace.mkdir(parents=True)
    server_dir = root / "run" / alias / "server"
    server_dir.mkdir()
    environment = dict(os.environ, PYTHONPATH=str(package.parent) + os.pathsep + os.getenv("PYTHONPATH", ""))
    runtime = server = None
    checks = []
    token = None

    def check(name, passed, **data):
        checks.append({"name": name, "passed": bool(passed), **data})
        print(json.dumps(checks[-1]), flush=True)
        if not passed:
            raise AssertionError(name)

    def wait(predicate, seconds, description):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.1)
        raise TimeoutError(description)

    with (root / "smoke-processes.log").open("w") as output:
        try:
            runtime = subprocess.Popen([sys.executable, "-u", "-m", "minecraft_realtime.runtime",
                "--run-root", str(root), "--accept-eula", "--spectator-port", "0"],
                env=environment, stdout=output, stderr=subprocess.STDOUT)
            ready_file = root / "run_mc/runtime.json"
            wait(ready_file.exists, 30, "runtime readiness")
            ready = json.loads(ready_file.read_text())
            with socket.socket() as candidate:
                candidate.bind(("127.0.0.1", 0))
                port = candidate.getsockname()[1]
            base = f"http://127.0.0.1:{port}"
            parameters = {"session_id": alias, "socket_path": ready["socket_path"],
                          "minecraft_task_timeout_seconds": 8, "capture_fps": 30, "record_fps": 1}
            server = subprocess.Popen([sys.executable, str(server_source), "--environment", "minecraft_realtime",
                "--game-id", "craft_wooden_pickaxe", "--game-alias", alias, "--port", str(port),
                "--parameters", json.dumps(parameters)],
                env=dict(environment, ENV_SERVER_LOG_PATH=str(server_dir / "server.log")),
                stdout=output, stderr=subprocess.STDOUT)

            def call(path, data=None):
                headers = {"Content-Type": "application/json"}
                if token:
                    headers["X-Session-Token"] = token
                with urlopen(Request(base + path, headers=headers,
                    data=None if data is None else json.dumps(data).encode()), timeout=700) as response:
                    return json.load(response)

            def healthy():
                try:
                    return call("/health")["status"] == "ok"
                except OSError:
                    return False

            wait(healthy, 20, "environment server readiness")
            response = call("/game/start", {"game_id": alias})
            token = response["session"]["token"]
            check("shared server starts minecraft_realtime", not response["session"]["terminal"])
            image = workspace / "latest_frame.jpg"
            content = image.read_bytes()
            check("complete workspace JPEG", content.startswith(b"\xff\xd8") and content.endswith(b"\xff\xd9"))
            call("/game/action", {"action": {"id": "key_down", "key": "shift"}})
            writes = set()
            until = time.monotonic() + 2
            while time.monotonic() < until:
                writes.add(image.stat().st_mtime_ns)
                time.sleep(.005)
            check("frames update without actions", len(writes) >= 10,
                  writes_in_two_seconds=len(writes), approximate_fps=len(writes) / 2)
            session = root / "run_mc/sessions" / alias
            result_path = session / "result.json"
            wait(result_path.exists, 15, "task timeout")
            verdict = json.loads(result_path.read_text())
            check("timeout is latched while idle", verdict["reason"] == "minecraft_task_timeout" and not verdict["success"])
            before = image.stat().st_mtime_ns
            time.sleep(.5)
            check("frames continue after timeout", image.stat().st_mtime_ns > before)
            check("shared server still nonterminal", not call("/game/last-step")["session"]["terminal"])
            events = [json.loads(line) for line in (session / "input-events/events.jsonl").read_text().splitlines()]
            check("held key survives timeout", events[-1]["type"] == "action_applied"
                  and "shift" in events[-1]["data"]["held_keys"])
            response = call("/game/action", {"action": {"id": "mouse_move", "dx": 5, "dy": 0}})
            check("next action reports terminal", response["session"]["terminal"])
            check("failed result remains unchanged", json.loads(result_path.read_text()) == verdict)
            events = [json.loads(line) for line in (session / "events.jsonl").read_text().splitlines()]
            check("one timeout event", sum(event.get("type") == "timeout" for event in events) == 1)
            check("sparse recording exists", len(list((session / "frames").glob("*.jpg"))) >= 5)
            with urlopen(ready["spectator_url"]) as response:
                check("spectator landing page", response.status == 200 and b"/view" in response.read())
            try:
                urlopen(Request(ready["spectator_url"] + f"/sessions/{alias}/view", data=b"{}"))
            except HTTPError as error:
                check("spectator rejects control requests", error.code == 405)
            else:
                check("spectator rejects control requests", False)
        finally:
            (root / "smoke-report.json").write_text(json.dumps({"session_id": alias, "checks": checks}, indent=2) + "\n")
            for process in (server, runtime):
                if process is not None and process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=180)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
