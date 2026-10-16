"""Explicit test doubles. They do NOT implement Docker or a real terminal."""
from __future__ import annotations

import asyncio
import os
import base64
import json
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from terminal_bench.description import environment_description
from terminal_bench.protocol import RuntimeFault, observation, terminal_observation
from terminal_bench.records import atomic_json


class ScriptedEnvironment:
    def __init__(self, user="agent"):
        self.default_user = user
        self.calls = []
        self.panes = {}
        self.next_pane = 0
        self.file_data = b"\x00\xffexample\n"
        self.file_return_code = 0
        self.tmux_available = True

    async def exec(self, command, user=None, timeout_sec=None):
        self.calls.append({"command": command, "user": user, "timeout_sec": timeout_sec})
        result = SimpleNamespace(stdout="", stderr="", return_code=0)
        if command.startswith("command -v"):
            result.return_code = 0 if self.tmux_available else 1
            return result
        argv = shlex.split(command)
        if argv == ["pwd"]:
            result.stdout = "/app\n"
        elif argv[0] == "bash":
            result.return_code = self.file_return_code
            result.stdout = base64.b64encode(self.file_data).decode()
        elif argv[0] == "tmux":
            args = argv[5:]  # tmux -L <socket> -f /dev/null
            if "new-session" in args:
                pane = f"%{self.next_pane}"
                self.next_pane += 1
                index = args.index("new-session")
                spec = args[index:]
                self.panes[pane] = {
                    "columns": int(spec[spec.index("-x") + 1]),
                    "rows": int(spec[spec.index("-y") + 1]),
                    "input": bytearray(), "keys": [], "screen": "ready$\n",
                    "dead": 0, "history": 0,
                }
                result.stdout = pane + "\n"
            elif args[0] == "resize-window":
                pass
            elif args[0] in {"display-message", "capture-pane", "send-keys"}:
                pane = args[args.index("-t") + 1]
                if pane not in self.panes:
                    result.return_code = 1
                    return result
                state = self.panes[pane]
                if args[0] == "display-message":
                    result.stdout = f'{state["columns"]}|{state["rows"]}|0|0|{state["dead"]}|{state["history"]}|0\n'
                elif args[0] == "capture-pane":
                    result.stdout = state["screen"]
                elif "-H" in args:
                    hex_args = args[args.index("-t") + 2:]
                    state["input"].extend(bytes(int(value, 16) for value in hex_args))
                else:
                    state["keys"].append(args[-1])
        else:
            raise AssertionError(f"Unexpected mocked command: {command}")
        return result


class FakeSession:
    """Used only for wire-protocol and manager tests; reward is deliberately fake."""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.closed = False
        self.ready = False
        self.close_task = None
        self.close_count = 0
        self.start_count = 0
        self.terminal_count = 1
        self.finished = False
        self.calls = []

    def screen(self, tid="t1"):
        return observation({"screen.txt": "fake terminal output\n",
                            "terminal.json": json.dumps({"terminal_id": tid,
                              "columns": self.settings.columns, "rows": self.settings.rows})})

    async def start(self):
        self.start_count += 1
        self.ready = True
        instruction = (self.task_path / "instruction.md").read_text()
        return {"session_id": self.session_id, "observation": self.screen(),
                "environment_description": environment_description(instruction,
                    self.settings.max_file_bytes)}

    async def move(self, action):
        self.calls.append(action)
        if self.finished:
            raise RuntimeFault("terminal", "Already finished")
        kind = action["id"]
        if "terminal_id" in action and action["terminal_id"] not in {
            f"t{i}" for i in range(1, self.terminal_count + 1)
        }:
            raise RuntimeFault("unknown_terminal", "Unknown terminal")
        if kind == "terminal_new":
            self.terminal_count += 1
            result = self.screen(f"t{self.terminal_count}")
        elif kind == "get_file":
            result = observation({"file.bin": b"\0\xffbinary\n", "file.json": "{}"})
        elif kind == "finish":
            self.finished = True
            self.request_close(reason="finish")
            result = terminal_observation()
        else:
            result = self.screen(action.get("terminal_id", "t1"))
        return {"observation": result}

    def request_close(self, *, reason="agent_stopped"):
        if self.close_task is None:
            self.close_task = asyncio.create_task(self.close(reason=reason))

    async def wait_finished(self):
        while not self.closed:
            await asyncio.sleep(0.01)

    async def close(self, *, reason="agent_stopped"):
        if not self.closed:
            self.close_count += 1
            self.closed = True
            path = self.output_dir / self.session_id / "result.json"
            if not path.exists():
                atomic_json(path, {
                    "status": "finished" if self.ready else "aborted",
                    "verification_status": "finished" if self.ready else "not_run",
                    "termination_reason": reason, "rewards": {"reward": 1.0} if self.ready else None,
                    "test_double": True, "session_id": self.session_id, "game_id": self.task_id,
                    "benchmark": self.benchmark, "task_name": self.task_name, "worker_pid": os.getpid(),
                })
