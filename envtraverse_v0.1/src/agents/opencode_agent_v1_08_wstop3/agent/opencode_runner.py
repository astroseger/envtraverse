"""Run one OpenCode CLI turn at a time, preserving its native session."""
from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import Thread, current_thread, main_thread
from typing import TextIO


class OpenCodeRunner:
    def __init__(self, work_dir: str | Path, log_file: str | Path,
                 model: str | None = None, reasoning_effort: str | None = None,
                 on_session_started: Callable[[str], None] | None = None,
                 thinking: bool = False, opencode_agent: str | None = None):
        self.work_dir = Path(work_dir).resolve()
        self.log_file = Path(log_file)
        self.stderr_log_file = self.log_file.with_name("opencode_stderr.log")
        self.events_log_file = self.log_file.with_name("opencode_events.jsonl")
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.thinking = thinking
        self.opencode_agent = opencode_agent
        self.on_session_started = on_session_started
        self.current_session_id: str | None = None
        self._model_argument()  # Reject invalid combinations before starting a process.

    def _model_argument(self) -> str | None:
        model = self.model
        if model is None:
            if self.reasoning_effort is not None:
                raise ValueError("reasoning_effort requires an explicit model")
            return None
        if not model or any(c.isspace() for c in model):
            raise ValueError("model must be nonempty and contain no whitespace")
        if "/" not in model:
            model = "openai/" + model
        if not model.startswith("openai/") or not model.removeprefix("openai/"):
            raise ValueError("ChatGPT accounts require an openai/<model> selection")
        if self.reasoning_effort is not None:
            if not self.reasoning_effort or any(c.isspace() for c in self.reasoning_effort):
                raise ValueError("reasoning_effort must be nonempty and contain no whitespace")
            if "#" in model or "#" in self.reasoning_effort:
                raise ValueError("Specify a model variant or reasoning_effort, not both")
            model += "#" + self.reasoning_effort
        if model.count("#") > 1 or model.endswith("#"):
            raise ValueError("Invalid model variant")
        return model

    def new_session(self) -> None:
        self.current_session_id = None

    def resume_session(self, session_id: str) -> None:
        session_id = session_id.strip()
        if not session_id or any(c.isspace() for c in session_id):
            raise ValueError("session_id must be nonempty and contain no whitespace")
        self.current_session_id = session_id

    def send(self, prompt: str) -> None:
        command = ["opencode", "run", "--standalone", "--auto", "--format", "json"]
        model = self._model_argument()
        if model is not None:
            command += ["--model", model]
        if self.thinking:
            command.append("--thinking")
        if self.opencode_agent is not None:
            command += ["--agent", self.opencode_agent]
        environment = dict(os.environ, PWD=str(self.work_dir))
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        if self.current_session_id is not None:
            # --session creates missing IDs. Recovery must instead fail if the
            # account DB no longer contains the saved conversation.
            with self.stderr_log_file.open("a", encoding="utf-8") as errors:
                subprocess.run(
                    ["opencode", "api", "session.get", "--param",
                     f"sessionID={self.current_session_id}", "--standalone"],
                    stdout=subprocess.DEVNULL, stderr=errors, stdin=subprocess.DEVNULL,
                    cwd=self.work_dir, env=environment, check=True, timeout=60,
                )
            command += ["--session", self.current_session_id]
        self._run_command_once(command, prompt, environment)
        if self.current_session_id is None:
            raise RuntimeError("OpenCode did not report a sessionID for the new session")

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()

    def _run_command_once(self, command: list[str], prompt: str, environment: dict[str, str]) -> None:
        with (self.log_file.open("a", encoding="utf-8") as progress,
              self.events_log_file.open("a", encoding="utf-8") as events,
              self.stderr_log_file.open("a", encoding="utf-8") as errors):
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=self.work_dir, env=environment, start_new_session=True,
                text=True, encoding="utf-8", errors="replace",
            )
            assert process.stdin is not None and process.stdout is not None and process.stderr is not None
            thread = Thread(target=self._stream_stderr, args=(process.stderr, errors), daemon=True)
            thread.start()
            previous_handler = None
            if current_thread() is main_thread():
                previous_handler = signal.getsignal(signal.SIGTERM)
                signal.signal(signal.SIGTERM, self._interrupt)
            reported_error = False
            try:
                process.stdin.write(prompt)
                process.stdin.close()
                for line in process.stdout:
                    events.write(line)
                    events.flush()
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    session_id = event.get("sessionID")
                    if isinstance(session_id, str) and session_id:
                        if self.current_session_id is None:
                            self.resume_session(session_id)
                            if self.on_session_started is not None:
                                self.on_session_started(session_id)
                        elif session_id != self.current_session_id:
                            raise RuntimeError("OpenCode emitted an event for a different session")
                    kind = event.get("type")
                    if kind == "error":
                        reported_error = True
                    # Retry/background-wait diagnostics must not keep the
                    # supervisor's agent.log inactivity clock alive.
                    if kind in {"step_start", "step_finish", "tool_use", "text", "reasoning"}:
                        progress.write(line)
                        progress.flush()
                return_code = process.wait()
            except BaseException:
                self._stop(process)
                raise
            finally:
                if previous_handler is not None:
                    signal.signal(signal.SIGTERM, previous_handler)
                if not process.stdin.closed:
                    process.stdin.close()
                thread.join()
                process.stdout.close()
                process.stderr.close()
        if return_code:
            raise subprocess.CalledProcessError(return_code, command)
        if reported_error:
            raise RuntimeError("OpenCode reported an error; see opencode_events.jsonl")

    @staticmethod
    def _interrupt(_signum, _frame):
        raise KeyboardInterrupt

    @staticmethod
    def _stream_stderr(stream: TextIO, output: TextIO) -> None:
        for line in stream:
            output.write(line)
            output.flush()
            sys.stderr.write(line)
            sys.stderr.flush()
