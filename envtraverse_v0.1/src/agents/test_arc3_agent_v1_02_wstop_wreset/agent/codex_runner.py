#!/usr/bin/env python3

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from threading import Thread
from typing import Any, TextIO


class CodexRunner:
    def __init__(
        self,
        work_dir: str | Path,
        log_file: str | Path,
        model: str = "gpt-5.5",
        reasoning_effort: str = "medium",
        on_thread_started: Callable[[str], None] | None = None,
    ):
        self.work_dir = Path(work_dir)
        self.log_file = Path(log_file)
        self.stderr_log_file = self.log_file.with_name("codex_stderr.log")
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.on_thread_started = on_thread_started
        self.current_thread_id: str | None = None

    def new_session(self) -> None:
        self.current_thread_id = None

    def resume_session(self, thread_id: str) -> None:
        normalized_thread_id = thread_id.strip()
        if not normalized_thread_id:
            raise ValueError("thread_id must not be empty")
        self.current_thread_id = normalized_thread_id

    def send(self, prompt: str) -> None:
        if self.current_thread_id is None:
            self._run_command_once(
                [
                    "codex",
                    "-m",
                    self.model,
                    "-c",
                    f"model_reasoning_effort={self.reasoning_effort}",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "exec",
                    "--cd",
                    str(self.work_dir),
                    "--json",
                ],
                prompt,
            )
            if self.current_thread_id is None:
                raise RuntimeError("Codex did not report a thread_id for the new session")
            return

        self._run_command_once(
            [
                "codex",
                "-m",
                self.model,
                "-c",
                f"model_reasoning_effort={self.reasoning_effort}",
                "--dangerously-bypass-approvals-and-sandbox",
                "exec",
                "--cd",
                str(self.work_dir),
                "resume",
                self.current_thread_id,
                "--json",
            ],
            prompt,
        )

    def _run_command_once(self, command: list[str], prompt: str) -> None:
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

        with (
            self.log_file.open("a", encoding="utf-8") as log_fh,
            self.stderr_log_file.open("a", encoding="utf-8") as stderr_log_fh,
        ):
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None

            # Drain stderr independently, even while waiting for stdout or writing
            # the prompt. Diagnostics must not update the watchdog's agent.log.
            stderr_thread = Thread(
                target=self._stream_stderr,
                args=(process.stderr, stderr_log_fh),
                name="codex-stderr",
                daemon=True,
            )
            stderr_thread.start()
            try:
                process.stdin.write(prompt)
                process.stdin.close()

                for line in process.stdout:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    log_fh.write(line)
                    log_fh.flush()
                    # Once the thread ID is known, events only need to be logged.
                    if self.current_thread_id is not None:
                        continue
                    stripped = line.strip()
                    if not stripped:
                        continue

                    try:
                        payload = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue

                    if isinstance(payload, dict):
                        self._capture_thread_id(payload)

                return_code = process.wait()
            except BaseException:
                # Do not leave Codex running if local prompt/log persistence fails.
                process.kill()
                process.wait()
                raise
            finally:
                stderr_thread.join()
                process.stdout.close()
                process.stderr.close()

        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, command)

    @staticmethod
    def _stream_stderr(stream: TextIO, log_fh: TextIO) -> None:
        for line in stream:
            log_fh.write(line)
            log_fh.flush()
            sys.stderr.write(line)
            sys.stderr.flush()

    def _capture_thread_id(self, payload: dict[str, Any]) -> None:
        if self.current_thread_id is not None:
            return

        thread_id = self._extract_thread_id(payload)
        if thread_id is None:
            return

        self.current_thread_id = thread_id
        if self.on_thread_started is not None:
            self.on_thread_started(thread_id)

    def _extract_thread_id(self, payload: Any) -> str | None:
        if isinstance(payload, dict):
            thread_id = payload.get("thread_id")
            if isinstance(thread_id, str):
                return thread_id

            for value in payload.values():
                thread_id = self._extract_thread_id(value)
                if thread_id:
                    return thread_id

        if isinstance(payload, list):
            for value in payload:
                thread_id = self._extract_thread_id(value)
                if thread_id:
                    return thread_id

        return None
