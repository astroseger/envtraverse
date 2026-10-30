"""Host process serving a small RPC protocol over a private Unix socket."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import json
import logging
import os
from pathlib import Path
import signal
import math
import struct
from typing import Any, Callable

from . import __version__
from .actions import validate_action
from .protocol import (MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, PROTOCOL_VERSION,
                       RuntimeFault, loads, pack)
from .settings import TerminalSettings
from .catalog import BenchmarkCatalog, add_catalog_arguments, catalog_from_args
from .records import atomic_json, error_result, now, session_key

LOGGER = logging.getLogger(__name__)


class RuntimeService:
    def __init__(self, *, socket_path: Path, output_dir: Path, runs_dir: Path,
                 catalog: BenchmarkCatalog | None = None,
                 settings: TerminalSettings = TerminalSettings(),
                 environment_type: str = "docker", environment_kwargs: dict[str, Any] | None = None,
                 socket_mode: int = 0o600, session_factory: Callable | None = None,
                 cleanup_grace_seconds: float = 120,
                 build_timeout_seconds: float = 3600):
        # Preserve a short relative pathname: this checkout's absolute socket
        # path can exceed sockaddr_un.sun_path even though the directory is valid.
        self.socket_path = socket_path
        self.catalog = catalog or BenchmarkCatalog()
        self.output_dir = output_dir.absolute()
        # run.sh starts before run/ exists; resolve its location without creating it.
        self.runs_dir = runs_dir.resolve()
        self.settings = settings
        self.environment_type = environment_type
        self.environment_kwargs = environment_kwargs or {}
        if socket_mode not in {0o600, 0o660}:
            raise ValueError("Socket mode must be 0600 or 0660")
        for value in (cleanup_grace_seconds, build_timeout_seconds):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Cleanup grace values must be finite and positive")
        self.cleanup_grace_seconds = cleanup_grace_seconds
        self.build_timeout_seconds = build_timeout_seconds
        self.socket_mode = socket_mode
        if session_factory is None:
            from .process_session import ProcessSession
            session_factory = ProcessSession
        self.session_factory = session_factory
        self.sessions: dict[str, Any] = {}
        self.completions: dict[str, asyncio.Task] = {}
        self.outcomes: dict[str, str] = {}
        self.owners: dict[str, str] = {}
        self.connections: set[asyncio.Task] = set()
        self.server: asyncio.Server | None = None
        self.stopping = False
        self.socket_inode: tuple[int, int] | None = None

    async def start(self) -> None:
        if self.server is not None:
            raise RuntimeError("Runtime already started")
        self.socket_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if os.path.lexists(self.socket_path):
            raise RuntimeError("Socket path exists. Verify no runtime owns it before removing a stale socket.")
        self.output_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        old_umask = os.umask(0o077)
        try:
            self.server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path))
        finally:
            os.umask(old_umask)
        info = self.socket_path.lstat()
        self.socket_inode = (info.st_dev, info.st_ino)
        os.chmod(self.socket_path, self.socket_mode)
        LOGGER.info("Runtime ready at %s (tasks are prepared only on start)", self.socket_path)

    async def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("version") != PROTOCOL_VERSION:
            raise RuntimeFault("bad_request", "Unsupported protocol version")
        method = request.get("method")
        if method == "health":
            return {"runtime_version": __version__, "protocol_version": PROTOCOL_VERSION,
                    "ready": not self.stopping}
        if self.stopping:
            raise RuntimeFault("unavailable", "Runtime is shutting down")
        if method not in {"start", "action", "close"}:
            raise RuntimeFault("bad_request", "Unknown runtime method")
        try:
            session_id = session_key(request.get("session_id"))
            owner = session_key(request.get("owner_token"))
        except ValueError as exc:
            raise RuntimeFault("bad_request", str(exc)) from exc
        if method == "start":
            directory = self.output_dir / session_id
            if session_id in self.owners or directory.exists():
                raise RuntimeFault("duplicate_session", "Session ID already exists; never reuse an execution ID")
            # Atomic reservation also prevents reuse after supervisor restarts.
            try:
                directory.mkdir(mode=0o700, exist_ok=False)
            except FileExistsError as exc:
                raise RuntimeFault("duplicate_session", "Session ID already exists") from exc
            self.owners[session_id] = owner
            (directory / "runtime.log").touch(mode=0o600)
            game_id = request.get("game_id")
            manifest = {"session_id": session_id, "game_id": game_id,
                        "accepted_at": now(), "runtime_version": __version__,
                        "environment_type": self.environment_type,
                        "columns": self.settings.columns, "rows": self.settings.rows}
            atomic_json(directory / "session.json", manifest)
            try:
                spec = self.catalog.resolve(game_id)
                manifest.update(benchmark=spec.benchmark, task_name=spec.task_name,
                                task_path=str(spec.path), source=spec.source)
                # Hash current local task files, including local changes, in the worker.
                atomic_json(directory / "session.json", manifest)
                session = self.session_factory(
                    session_id=session_id, task_id=spec.game_id, task_path=spec.path,
                    output_dir=self.output_dir, settings=self.settings,
                    workspace_dir=self.runs_dir / session_id / "run" / "agent_run",
                    benchmark=spec.benchmark, task_name=spec.task_name,
                    environment_type=self.environment_type, environment_kwargs=self.environment_kwargs,
                    cleanup_grace_seconds=self.cleanup_grace_seconds,
                    build_timeout_seconds=self.build_timeout_seconds,
                )
                # Registered before awaiting setup: independent close can cancel it.
                self.sessions[session_id] = session
                self.completions[session_id] = asyncio.create_task(
                    self._watch_session(session_id, session), name=f"completion-{session_id}",
                )
                result = await session.start()
                public_identity = {"session_id": session_id}
                result["observation"]["files"]["session.json"] = {"text": json.dumps(public_identity, indent=2)}
                return result
            except BaseException as exc:
                session = self.sessions.get(session_id)
                if session is not None:
                    await session.close()
                error_result(directory, session_id=session_id, game_id=game_id,
                             benchmark=manifest.get("benchmark"), task_name=manifest.get("task_name"),
                             exception_type=type(exc).__name__, note="Session initialization failed; see runtime.log")
                self._report_completed(session_id)
                raise
        if session_id in self.owners and self.owners[session_id] != owner:
            raise RuntimeFault("not_owner", "This manager does not own that session")
        session = self.sessions.get(session_id)
        if method == "close":
            if session is not None:
                session.request_close()
            # Acknowledge immediately; the host owns grading and worker cleanup.
            return {"closed": True}
        if session is None:
            raise RuntimeFault("unknown_session", "Unknown runtime session")
        action = validate_action(request.get("action"))
        return await session.move(action)

    async def _watch_session(self, session_id: str, session: Any) -> None:
        try:
            await session.wait_finished()
        except Exception:
            LOGGER.exception("Session finalization failed: %s", session_id)
            error_result(self.output_dir / session_id, session_id=session_id, game_id=session.task_id,
                         exception_type="FinalizationError", note="Inspect the host runtime log", cleanup_required=True)
        self._report_completed(session_id)

    def _report_completed(self, session_id: str) -> None:
        if session_id in self.outcomes:
            return
        result = json.loads((self.output_dir / session_id / "result.json").read_text())
        status = result.get("status")
        outcome = ("validated" if status == "finished" and result.get("verification_status") == "finished"
                   else "aborted" if status == "aborted" else "error")
        self.outcomes[session_id] = outcome
        LOGGER.info("Task %s: %s; results: %s", session_id, outcome, self.output_dir / session_id)
        counts = {kind: sum(value == kind for value in self.outcomes.values())
                  for kind in ("validated", "error", "aborted")}
        active = len(self.owners) - len(self.outcomes)
        LOGGER.info("%s tasks validated. Errors: %s. Aborted: %s. Active: %s.%s",
                    counts["validated"], counts["error"], counts["aborted"], active,
                    " No active sessions." if active == 0 else "")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.connections.add(task)
        try:
            try:
                # Only bound time spent READING a request, not task setup/grading.
                prefix = await asyncio.wait_for(reader.readexactly(4), timeout=30)
                length = struct.unpack("!I", prefix)[0]
                if length > MAX_REQUEST_BYTES:
                    raise RuntimeFault("bad_request", "Request is too large")
                body = await asyncio.wait_for(reader.readexactly(length), timeout=30)
                request = loads(body)
                result = await self.dispatch(request)
                response = {"version": PROTOCOL_VERSION, "ok": True, "result": result}
            except RuntimeFault as exc:
                response = {"version": PROTOCOL_VERSION, "ok": False,
                            "error": {"code": exc.code, "message": str(exc)}}
            except (ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.TimeoutError):
                response = {"version": PROTOCOL_VERSION, "ok": False,
                            "error": {"code": "bad_request", "message": "Malformed or incomplete request"}}
            except Exception:
                LOGGER.exception("Runtime request failed")
                response = {"version": PROTOCOL_VERSION, "ok": False,
                            "error": {"code": "environment_error", "message": "Runtime operation failed; see host logs"}}
            try:
                data = pack(response, MAX_RESPONSE_BYTES)
            except ValueError:
                data = pack({"version": PROTOCOL_VERSION, "ok": False,
                             "error": {"code": "environment_error", "message": "Response exceeds size limit"}},
                            MAX_RESPONSE_BYTES)
            writer.write(data)
            await writer.drain()
        except (ConnectionError, BrokenPipeError):
            LOGGER.warning("Client disconnected; no state-changing request is automatically retried")
        finally:
            writer.close()
            with suppress(ConnectionError, BrokenPipeError):
                await writer.wait_closed()
            self.connections.discard(task)

    async def stop(self) -> None:
        if self.stopping:
            return
        self.stopping = True
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        # Prepared tasks are submitted for grading, then all workers are drained.
        results = await asyncio.gather(*(s.close() for s in self.sessions.values()), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                LOGGER.error("Session cleanup failed: %r", result)
        await asyncio.gather(*self.completions.values(), return_exceptions=True)
        if self.connections:
            pending = list(self.connections)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        if self.socket_inode is not None:
            with suppress(FileNotFoundError):
                current = self.socket_path.lstat()
                if (current.st_dev, current.st_ino) == self.socket_inode:
                    self.socket_path.unlink()


async def _serve(args: argparse.Namespace) -> None:
    from .harbor_runner import check_harbor
    # Import/version check only. Trial preparation and execution are in child processes.
    version = check_harbor()
    settings = TerminalSettings(columns=args.columns, rows=args.rows,
                                install_tmux=not args.no_install_tmux)
    backend_kwargs = json.loads(args.environment_kwargs)
    if not isinstance(backend_kwargs, dict):
        raise ValueError("environment-kwargs must be a JSON object")
    service = RuntimeService(
        socket_path=args.socket, catalog=catalog_from_args(args), output_dir=args.output, runs_dir=args.runs_dir,
        settings=settings,
        environment_type=args.environment_type, environment_kwargs=backend_kwargs,
        socket_mode=int(args.socket_mode, 8),
        cleanup_grace_seconds=args.cleanup_grace_seconds,
        build_timeout_seconds=args.build_timeout_seconds,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    LOGGER.info("Using Harbor %s", version)
    try:
        await service.start()
        await stop.wait()
    finally:
        await service.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--runs-dir", type=Path, required=True,
                        help="Host run/ directory containing <session>/run/agent_run workspaces")
    add_catalog_arguments(parser)
    parser.add_argument("--output", type=Path, default=Path("tb-results"))
    parser.add_argument("--cleanup-grace-seconds", type=float, default=120)
    parser.add_argument("--environment-type", default="docker", help="Harbor backend, default docker")
    parser.add_argument("--environment-kwargs", default="{}", help="Operator-supplied Harbor backend kwargs as JSON")
    parser.add_argument("--columns", type=int, default=160)
    parser.add_argument("--rows", type=int, default=40)
    parser.add_argument("--no-install-tmux", action="store_true")
    parser.add_argument("--socket-mode", choices=["600", "660"], default="600")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--build-timeout-seconds", type=float, default=3600, help="Minimum infrastructure build allowance; does not change solving limits")
    args = parser.parse_args()
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file is not None:
        handlers.append(logging.FileHandler(args.log_file, mode="x", encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    try:
        asyncio.run(_serve(args))
    except (RuntimeError, ValueError, OSError) as exc:
        parser.exit(1, f"Runtime startup failed: {exc}\n")


if __name__ == "__main__":
    main()
