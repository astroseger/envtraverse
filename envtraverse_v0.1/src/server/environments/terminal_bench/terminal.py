"""tmux-backed screens in Harbor's main sandbox, never in the runtime host.

Only validated tmux keys and hex-encoded text enter tmux's command parser.
All shell execution is through the Harbor environment object.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import shlex
from typing import Any
from uuid import uuid4

from .actions import KEY_ALIASES, validate_action
from .protocol import RuntimeFault, observation
from .settings import TerminalSettings


class TmuxTerminals:
    def __init__(self, environment: Any, settings: TerminalSettings):
        self.environment = environment
        self.settings = settings
        self.socket_name = "ua_" + uuid4().hex
        self.panes: dict[str, str] = {}
        self.initial_cwd = "/"
        self.user = environment.default_user

    async def _raw(self, command: str, *, timeout: int = 30, user: Any = None) -> Any:
        return await self.environment.exec(
            command=command, user=self.user if user is None else user,
            timeout_sec=timeout,
        )

    async def _checked(self, argv: list[str], *, timeout: int = 30) -> str:
        result = await self._raw(shlex.join(argv), timeout=timeout)
        if result.return_code != 0:
            # Full command diagnostics are host-only (Harbor/runtime logs).
            raise RuntimeError(f"Sandbox tool returned {result.return_code}: {result.stderr}")
        return result.stdout or ""

    def _tmux(self, *args: str) -> list[str]:
        return ["tmux", "-L", self.socket_name, "-f", "/dev/null", *args]

    async def setup(self) -> None:
        result = await self._raw("command -v tmux", timeout=15)
        if result.return_code != 0:
            if not self.settings.install_tmux:
                raise RuntimeError("tmux is absent; enable install_tmux or supply it in the task image")
            # Fixed operator-controlled dependency installation, never an agent command.
            install = '''set -eu
if command -v apt-get >/dev/null 2>&1; then
  apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tmux
elif command -v apk >/dev/null 2>&1; then
  apk add --no-cache tmux
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y tmux
else
  echo 'No supported package manager for tmux' >&2; exit 1
fi'''
            installed = await self._raw(install, timeout=240, user="root")
            if installed.return_code != 0:
                raise RuntimeError(f"tmux installation failed: {installed.stderr}")
        checked = await self._raw("command -v bash && command -v tmux && command -v head && command -v base64")
        if checked.return_code != 0:
            raise RuntimeError("The task image must provide bash, tmux, head and base64")
        cwd = (await self._checked(["pwd"])).strip()
        if not cwd.startswith("/") or "\n" in cwd or "\0" in cwd:
            raise RuntimeError("Could not determine the sandbox's initial working directory")
        self.initial_cwd = cwd

    async def new(self) -> dict[str, Any]:
        tid = f"t{len(self.panes) + 1}"
        # A single tmux command list: configure history BEFORE creating the pane.
        # Semicolons here are trusted tmux separators, not shell separators.
        args = self._tmux(
            "start-server", ";",
            "set-option", "-g", "status", "off", ";",
            "set-option", "-g", "history-limit", str(self.settings.history_limit), ";",
            "set-option", "-g", "default-terminal", "xterm-256color", ";",
            "set-option", "-g", "remain-on-exit", "on", ";",
            "new-session", "-d", "-s", tid, "-x", str(self.settings.columns),
            "-y", str(self.settings.rows), "-c", self.initial_cwd,
            "-P", "-F", "#{pane_id}", "bash --noprofile --norc -i",
        )
        pane = (await self._checked(args)).strip()
        if not pane.startswith("%") or not pane[1:].isdigit():
            raise RuntimeError("tmux did not return a pane ID")
        self.panes[tid] = pane
        await self._checked(self._tmux("resize-window", "-t", tid,
            "-x", str(self.settings.columns), "-y", str(self.settings.rows)))
        result = await self.read(tid)
        # terminal.json already includes the ID; no implicit 'current' terminal.
        return result

    def _pane(self, tid: str) -> str:
        if tid not in self.panes:
            raise RuntimeFault("unknown_terminal", "Unknown terminal ID")
        return self.panes[tid]

    async def _metadata(self, tid: str) -> dict[str, Any]:
        pane = self._pane(tid)
        fmt = "#{pane_width}|#{pane_height}|#{cursor_x}|#{cursor_y}|#{pane_dead}|#{history_size}|#{alternate_on}"
        result = await self._raw(shlex.join(self._tmux("display-message", "-p", "-t", pane, fmt)))
        if result.return_code != 0:
            raise RuntimeFault("terminal_unavailable", "This terminal is unavailable; try terminal_new")
        try:
            columns, rows, x, y, dead, history, alternate = map(int, (result.stdout or "").strip().split("|"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Malformed tmux metadata") from exc
        return {
            "terminal_id": tid, "columns": columns, "rows": rows,
            "cursor": {"column": x, "row": y}, "closed": bool(dead),
            "history_lines_retained": history,
            "history_limit": self.settings.history_limit,
            "alternate_screen": bool(alternate),
            "stdout_stderr_combined": True,
        }

    async def read(self, tid: str, view: str = "screen", wait_ms: int = 0,
                   lines: int = 200) -> dict[str, Any]:
        pane = self._pane(tid)
        if wait_ms:
            await asyncio.sleep(wait_ms / 1000)
        metadata = await self._metadata(tid)
        args = self._tmux("capture-pane", "-p", "-t", pane)
        if view == "history":
            args += ["-S", "-"]
        text = await self._checked(args)
        returned_lines = text.splitlines(keepends=True)
        truncated = False
        if view == "history" and len(returned_lines) > lines:
            text = "".join(returned_lines[-lines:])
            truncated = True
        metadata.update({"view": view, "returned_lines": len(text.splitlines()),
                         "earlier_retained_lines_omitted": truncated,
                         "history_may_have_evicted_older_output": True})
        # Not an atomic transaction with the display-message capture above.
        return observation({f"{view}.txt": text,
                            "terminal.json": json.dumps(metadata, indent=2)})

    async def send(self, tid: str, keys: list[dict[str, str]]) -> dict[str, Any]:
        pane = self._pane(tid)
        if (await self._metadata(tid))["closed"]:
            raise RuntimeFault("invalid_action", "Terminal shell has exited; open a new terminal")
        for item in keys:
            if "key" in item:
                key = KEY_ALIASES.get(item["key"], item["key"])
                await self._checked(self._tmux("send-keys", "-t", pane, "--", key))
            else:
                # -H emits literal bytes. This avoids tmux's second command-parser
                # layer treating literal ';' as a command separator. UTF-8 is sent
                # unchanged, including newlines; no Enter is appended.
                data = item["text"].encode("utf-8")
                for start in range(0, len(data), 2048):
                    encoded = [f"{value:02x}" for value in data[start:start + 2048]]
                    await self._checked(self._tmux("send-keys", "-H", "-t", pane, *encoded))
        return observation({"input.json": json.dumps({
            "terminal_id": tid, "input_delivered": True,
            "note": "Input delivery is not command completion; use terminal_read.",
        }, indent=2)})

    async def get_file(self, path: str) -> dict[str, Any]:
        # Read as the same user as the terminal, NOT privileged docker cp/download.
        # The read is in the sandbox. head bounds output; pipefail catches read errors.
        script = (
            "set -o pipefail; p=" + shlex.quote(path) + "; "
            "[ -f \"$p\" ] && [ -r \"$p\" ] || exit 44; "
            f"head -c {self.settings.max_file_bytes + 1} -- \"$p\" | base64"
        )
        result = await self._raw(shlex.join(["bash", "-c", script]))
        if result.return_code != 0:
            raise RuntimeFault("invalid_action", "File is not a readable regular file or could not be read")
        try:
            data = base64.b64decode("".join((result.stdout or "").split()), validate=True)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("Invalid encoded file data from sandbox") from exc
        if len(data) > self.settings.max_file_bytes:
            raise RuntimeFault("invalid_action", "File exceeds configured retrieval limit")
        return observation({"file.bin": data, "file.json": json.dumps({
            "path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        }, indent=2)})

    async def perform(self, raw: dict[str, Any]) -> dict[str, Any]:
        action = validate_action(raw)
        match action["id"]:
            case "terminal_new":
                return await self.new()
            case "terminal_send":
                return await self.send(action["terminal_id"], action["keys"])
            case "terminal_read":
                return await self.read(action["terminal_id"], action.get("view", "screen"),
                                       action.get("wait_ms", 0), action.get("lines", 200))
            case "get_file":
                return await self.get_file(action["path"])
            case _:
                raise RuntimeFault("invalid_action", "finish is handled by the trial lifecycle")
