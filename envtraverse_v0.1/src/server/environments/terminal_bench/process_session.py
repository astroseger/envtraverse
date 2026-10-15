"""Supervisor-side session proxy: one fresh Python process, one private socketpair.

No Harbor code runs in the shared supervisor. Close bypasses the normal operation
lock, so a blocked start/action cannot prevent bounded cancellation of its worker.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import socket
import struct
import sys
from typing import Any

from .protocol import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, RuntimeFault, loads, pack, terminal_observation
from .records import atomic_json, error_result, now
from .settings import TerminalSettings

LOGGER = logging.getLogger(__name__)


class ProcessSession:
    def __init__(self, *, session_id: str, task_id: str, task_path: Path,
                 output_dir: Path, settings: TerminalSettings, workspace_dir: Path,
                 benchmark: str, task_name: str,
                 environment_type: str = "docker", environment_kwargs: dict[str, Any] | None = None,
                 cleanup_grace_seconds: float = 120, finish_grace_seconds: float = 7200,
                 worker_factory: str = "terminal_bench.harbor_runner:HarborSession",
                 build_timeout_seconds: float = 3600):
        self.session_id, self.task_id = session_id, task_id
        self.directory = output_dir / session_id  # Reserved by RuntimeService, not by the child.
        self.identity = {"benchmark": benchmark, "task_name": task_name}
        self.spec = {
            "session_id": session_id, "task_id": task_id, "task_path": str(task_path),
            "output_dir": str(output_dir), "settings": asdict(settings), **self.identity,
            "workspace_dir": str(workspace_dir),
            "environment_type": environment_type, "environment_kwargs": environment_kwargs or {},
            "build_timeout_seconds": build_timeout_seconds,
            # Operator/test injection only. This value is never accepted from network requests.
            "worker_factory": worker_factory,
        }
        self.cleanup_grace_seconds, self.finish_grace_seconds = cleanup_grace_seconds, finish_grace_seconds
        self.process: asyncio.subprocess.Process | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.reader_task: asyncio.Task | None = None
        self.launch_task: asyncio.Task | None = None
        self.close_task: asyncio.Task | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.sequence = 0
        self.operation_lock = asyncio.Lock()
        self.write_lock = asyncio.Lock()
        self.closing = False
        self.closed = False
        self.ready = False
        self.submitted = False
        self.close_reason = "agent_stopped"
        self.failed = False

    def _record_failure(self, error: str, note: str, *, cleanup_required: bool = True) -> None:
        error_result(self.directory, session_id=self.session_id, game_id=self.task_id,
                     exception_type=error, note=note, cleanup_required=cleanup_required, **self.identity)

    async def _launch(self) -> None:
        atomic_json(self.directory / "worker-spec.json", self.spec)
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        parent.setblocking(False)
        try:
            with (self.directory / "runtime.log").open("ab", buffering=0) as log:
                self.process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "terminal_bench.worker", "--fd", str(child.fileno()),
                    "--spec", str(self.directory / "worker-spec.json"),
                    pass_fds=(child.fileno(),), stdin=asyncio.subprocess.DEVNULL,
                    stdout=log, stderr=log,
                )
            child.close()
            self.reader, self.writer = await asyncio.open_connection(sock=parent)
            self.reader_task = asyncio.create_task(self._read_replies(), name=f"ipc-{self.session_id}")
            manifest = json.loads((self.directory / "session.json").read_text())
            manifest.update(worker_pid=self.process.pid, worker_started_at=now())
            atomic_json(self.directory / "session.json", manifest)
        except BaseException:
            parent.close()
            child.close()
            if self.process is not None and self.process.returncode is None:
                self.process.kill()
                await self.process.wait()
            self._record_failure("WorkerStartError", "Could not launch the session worker", cleanup_required=False)
            raise

    async def _read_replies(self) -> None:
        assert self.reader is not None
        failure = "Session worker ended before replying; inspect its result and runtime.log"
        try:
            while True:
                length = struct.unpack("!I", await self.reader.readexactly(4))[0]
                if length > MAX_RESPONSE_BYTES:
                    raise ValueError("Oversized worker response")
                reply = loads(await self.reader.readexactly(length))
                future = self.pending.pop(reply.get("request_id"), None)
                if future is not None and not future.done():
                    if reply.get("ok") is True:
                        future.set_result(reply["result"])
                    else:
                        fault = reply.get("error", {})
                        future.set_exception(RuntimeFault(fault.get("code", "worker_failed"),
                                                          fault.get("message", failure)))
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, ConnectionError, ValueError, KeyError):
            pass
        finally:
            self.failed = True
            # All waits are released even if the worker exits abruptly.
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeFault("worker_failed", failure))
            self.pending.clear()
            if not self.closing:
                # EOF may precede process reaping by a moment. It is no longer useful.
                if self.process is not None and self.process.returncode is None:
                    with suppress(ProcessLookupError):
                        self.process.terminate()
                    try:
                        await asyncio.wait_for(self.process.wait(), 5)
                    except asyncio.TimeoutError:
                        with suppress(ProcessLookupError):
                            self.process.kill()
                        await self.process.wait()
                self._record_failure("WorkerExited", failure)

    async def _rpc(self, method: str, **payload: Any) -> dict[str, Any]:
        if self.failed or self.writer is None:
            raise RuntimeFault("worker_failed", "Session worker is unavailable; inspect the saved result")
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        # Consume a later error even if a disconnected caller no longer awaits it.
        future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        self.pending[request_id] = future
        try:
            async with self.write_lock:
                self.writer.write(pack({"request_id": request_id, "method": method, **payload}, MAX_REQUEST_BYTES))
                await self.writer.drain()
            return await asyncio.shield(future)
        except (ConnectionError, BrokenPipeError) as exc:
            raise RuntimeFault("worker_failed", "Worker connection lost; input may have executed") from exc
        finally:
            # Outstanding futures are retained until a reply/EOF, not retried.
            if future.done():
                self.pending.pop(request_id, None)

    async def start(self) -> dict[str, Any]:
        async with self.operation_lock:
            if self.closing:
                raise RuntimeFault("start_failed", "Session was closed before startup")
            if self.launch_task is None:
                self.launch_task = asyncio.create_task(self._launch())
            await asyncio.shield(self.launch_task)
            if self.closing:
                raise RuntimeFault("start_failed", "Session was closed during startup")
            result = await self._rpc("start")
            self.ready = True
            return result

    async def move(self, action: dict[str, Any]) -> dict[str, Any]:
        async with self.operation_lock:
            if self.closing:
                return {"observation": terminal_observation()}
            if not self.ready:
                raise RuntimeFault("not_ready", "Session has not completed startup")
            if action["id"] == "finish":
                self.submitted = True
                self.request_close(reason="finish")
                return {"observation": terminal_observation()}
            result = await self._rpc("action", action=action)
            if result.get("observation", {}).get("terminal") is True:
                self.request_close()
            return result

    def _preserve_finalization(self) -> bool:
        if self.submitted or self.ready:
            return True
        try:
            lifecycle = json.loads((self.directory / "lifecycle.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return (isinstance(lifecycle, dict) and lifecycle.get("session_id") == self.session_id
                and lifecycle.get("preserve_finalization") is True)

    async def _wait_for_close(self) -> None:
        # A timeout can end solving while close is being dispatched. Recheck the
        # worker's private atomic lifecycle record instead of choosing a fixed
        # grace solely from whether the caller sent finish(). Once extended, the
        # grace is not shortened, and polling never restarts the absolute deadline.
        loop = asyncio.get_running_loop()
        started = loop.time()
        preserve = self._preserve_finalization()
        closing = asyncio.create_task(self._rpc("close", reason=self.close_reason))
        try:
            while not closing.done():
                preserve = preserve or self._preserve_finalization()
                grace = (max(self.cleanup_grace_seconds, self.finish_grace_seconds)
                         if preserve else self.cleanup_grace_seconds)
                remaining = started + grace - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError("Worker exceeded its cleanup/verification grace")
                await asyncio.wait((closing,), timeout=min(0.05, remaining))
            await closing
        finally:
            if not closing.done():
                closing.cancel()
            await asyncio.gather(closing, return_exceptions=True)

    async def _close_impl(self) -> None:
        forced = False
        try:
            if self.launch_task is not None:
                with suppress(Exception):
                    await asyncio.shield(self.launch_task)
            if self.process is not None and self.process.returncode is None and not self.failed:
                try:
                    await self._wait_for_close()
                    await asyncio.wait_for(self.process.wait(), timeout=5)
                except (asyncio.TimeoutError, RuntimeFault, ConnectionError):
                    forced = True
            if self.process is not None and self.process.returncode is None:
                forced = True
                with suppress(ProcessLookupError):
                    self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except asyncio.TimeoutError:
                    with suppress(ProcessLookupError):
                        self.process.kill()
                    await self.process.wait()
            if forced:
                self._record_failure("WorkerForcedTermination", "Worker exceeded cleanup grace or lost communication. "
                                     "Task containers may require manual cleanup; no automatic action replay.")
            elif not (self.directory / "result.json").exists():
                error_result(self.directory, session_id=self.session_id, game_id=self.task_id,
                             exception_type="SessionAborted", status="aborted", **self.identity,
                             note="Session closed before activation", cleanup_required=False)
        finally:
            if self.writer is not None:
                self.writer.close()
                with suppress(ConnectionError, BrokenPipeError):
                    await self.writer.wait_closed()
            if self.reader_task is not None:
                # Reader normally finishes on EOF; do not leave a background task.
                await asyncio.gather(self.reader_task, return_exceptions=True)
            self.closed = True

    def request_close(self, *, reason: str = "agent_stopped") -> None:
        # This must NOT acquire operation_lock: a blocked startup/action holds it.
        if self.close_task is None:
            self.close_reason = reason
            self.closing = True
            self.close_task = asyncio.create_task(self._close_impl())

    async def close(self) -> None:
        self.request_close()
        await asyncio.shield(self.close_task)

    async def wait_finished(self) -> None:
        # Native timeouts may finish a trial while the external agent is thinking.
        # Reap it and report completion without requiring another client action.
        while not self.closed:
            if self.close_task is not None or self.failed or (self.directory / "result.json").is_file():
                await self.close()
                return
            await asyncio.sleep(0.1)
