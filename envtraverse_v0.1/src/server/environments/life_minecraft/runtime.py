"""Host-only lifecycle, private Unix IPC, and a GET-only spectator service."""
from __future__ import annotations

import argparse
from functools import partial
import hmac
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import signal
import socketserver
import struct
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID

from .catalog import get_task
from .docker_backend import DockerBackend
from .protocol import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, PROTOCOL_VERSION, RuntimeFault, _recv_exact, loads, pack
from .recording import atomic_json
from .session import Session
from .settings import Settings
from .images import pin_images
from .network import RuntimeNetwork
from .resources import parse_resource_context

LOGGER = logging.getLogger(__name__)


def session_key(value) -> str:
    if not isinstance(value, str):
        raise RuntimeFault("bad_request", "session_id must be a canonical UUID")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise RuntimeFault("bad_request", "session_id must be a canonical UUID") from error
    if value.lower() not in (parsed.hex, str(parsed)):
        raise RuntimeFault("bad_request", "session_id must be a canonical UUID")
    return parsed.hex


class RuntimeService:
    def __init__(self, run_root: Path, *, max_sessions: int = 1, accept_eula: bool = False,
                 backend_factory=DockerBackend, clock=time.monotonic, network_factory=RuntimeNetwork):
        if type(max_sessions) is not int or max_sessions < 1:
            raise ValueError("max_sessions must be positive")
        self.run_root = run_root.resolve()
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.directory = self.run_root / "run_mc"
        self.directory.mkdir()
        (self.directory / "socket").mkdir()
        (self.directory / "sessions").mkdir()
        self.max_sessions, self.accept_eula = max_sessions, accept_eula
        self.backend_factory, self.clock = backend_factory, clock
        self.sessions: dict[str, Session] = {}
        # Reserve ownership before waiting for a slot. Closed identities remain
        # remembered, so an acknowledged close cannot race a delayed start into
        # launching an orphaned game.
        self.owners: dict[str, str] = {}
        self.pending: dict[str, str] = {}
        self.closed_requests: set[tuple[str, str]] = set()
        self.lock = threading.RLock()
        self.capacity_changed = threading.Condition(self.lock)
        self.stopping = False
        self.viewer_base = None
        self.cleanup_threads = []
        self.network = network_factory(self.directory)
        self.network_cleanup_error = None

    def prepare(self):
        """Create the shared network before accepting game requests."""
        with self.lock:
            if self.stopping:
                raise RuntimeError("Minecraft runtime is shutting down")
            self.network.create()

    def dispatch(self, request: dict):
        if request.get("version") != PROTOCOL_VERSION:
            raise RuntimeFault("bad_request", "Unsupported protocol version")
        method = request.get("method")
        fields = {"version", "method", "session_id", "owner_token"}
        if method == "start":
            fields |= {"game_id", "settings"}
            if "resources" in request:
                fields.add("resources")
        elif method == "action":
            fields.add("action")
        elif method != "close":
            raise RuntimeFault("bad_request", "Unknown runtime method")
        if set(request) != fields:
            raise RuntimeFault("bad_request", "Incorrect runtime request fields")
        key = session_key(request["session_id"])
        token = request["owner_token"]
        if not isinstance(token, str) or len(token) != 32 or any(c not in "0123456789abcdef" for c in token):
            raise RuntimeFault("bad_request", "Invalid owner token")
        if method == "start":
            return self.start(key, token, request["game_id"], request["settings"], request.get("resources"))
        with self.lock:
            session = self.sessions.get(key)
            if session is None:
                owner = self.owners.get(key)
                if owner is not None and not hmac.compare_digest(owner, token):
                    raise RuntimeFault("not_owner", "Session belongs to another manager")
                if method == "close":
                    # An unknown close is remembered only for this exact
                    # owner capability, never for another manager's identity.
                    self.closed_requests.add((key, token))
                    self.capacity_changed.notify_all()
                    return {"closed": True}
                if key in self.pending:
                    raise RuntimeFault("not_ready", "Minecraft session is waiting for capacity")
                raise RuntimeFault("unknown_session", "Unknown Minecraft session")
            if not hmac.compare_digest(session.owner_token, token):
                raise RuntimeFault("not_owner", "Session belongs to another manager")
        if method == "action":
            return session.action(request["action"])
        self.close_session(session)
        return {"closed": True}

    def start(self, key: str, token: str, game_id: str, raw_settings: dict, resources: dict | None = None):
        try:
            resources = parse_resource_context(resources, session_id=key)
            settings = Settings.from_dict(raw_settings)
            task = get_task(game_id)
            if settings.difficulty == "peaceful" and task.get("requires_hostiles"):
                raise ValueError("This task requires hostile mobs; choose easy, normal, or hard difficulty")
        except (ValueError, KeyError, TypeError) as error:
            raise RuntimeFault("bad_request", str(error)) from error
        with self.lock:
            if self.stopping:
                raise RuntimeFault("unavailable", "Runtime is shutting down")
            if (key, token) in self.closed_requests:
                raise RuntimeFault("cancelled", "Minecraft session was closed before startup")
            if key in self.owners:
                raise RuntimeFault("duplicate_session", "Session has already been started")
            self.owners[key] = token
            self.pending[key] = token
            try:
                while sum(not session.closed or session.cleanup_failed for session in self.sessions.values()) >= self.max_sessions:
                    self._check_pending_start(key, token)
                    # close acknowledges cleanup ownership immediately. A sequential
                    # runner can request its next game while Docker is still stopping
                    # the previous one; wait for that slot rather than rejecting it.
                    if not any(session.close_requested and not session.closed for session in self.sessions.values()):
                        if any(session.cleanup_failed for session in self.sessions.values()):
                            raise RuntimeFault("capacity", "Previous Minecraft session requires Docker cleanup; inspect host logs")
                        raise RuntimeFault("capacity", "Minecraft runtime concurrency limit reached")
                    self.capacity_changed.wait(timeout=1)
                self._check_pending_start(key, token)
                directory = self.directory / "sessions" / key
                if directory.exists():
                    raise RuntimeFault("duplicate_session", "Session has already been prepared")
                try:
                    directory.mkdir()
                    self.network.create()  # Idempotent for direct RuntimeService callers.
                    viewer = None if self.viewer_base is None else self.viewer_base + f"/sessions/{key}/view"
                    session = Session(session_id=key, owner_token=token, task=task, settings=settings,
                                      run_root=self.run_root, directory=directory, accept_eula=self.accept_eula,
                                      backend_factory=partial(self.backend_factory, network_name=self.network.name),
                                      clock=self.clock, viewer_url=viewer, resources=resources)
                except Exception as error:
                    LOGGER.exception("Session preparation failed: %s", key)
                    atomic_json(directory / "result.json", {"session_id": key, "task_id": game_id,
                        "status": "error", "success": False, "reason": "preparation_failed", "finished_at": time.time()})
                    raise RuntimeFault("start_failed", "Minecraft workspace preparation failed") from error
                self.sessions[key] = session
            finally:
                self.pending.pop(key, None)
        try:
            return session.start()
        except Exception as error:
            LOGGER.exception("Session startup failed: %s", key)
            self.close_session(session)
            raise RuntimeFault("start_failed", "Minecraft startup failed; inspect host run_mc logs") from error

    def _check_pending_start(self, key: str, token: str):
        if (key, token) in self.closed_requests:
            raise RuntimeFault("cancelled", "Minecraft session was closed before startup")
        if self.stopping:
            raise RuntimeFault("unavailable", "Runtime is shutting down")

    def close_session(self, session: Session):
        with self.lock:
            if session.close_requested:
                return
            session.close_requested = True
            def cleanup():
                try:
                    session.close()
                finally:
                    with self.capacity_changed:
                        self.capacity_changed.notify_all()
            thread = threading.Thread(target=cleanup, name="mc-close-" + session.session_id[:8], daemon=True)
            self.cleanup_threads.append(thread)
            thread.start()

    def shutdown(self):
        with self.lock:
            self.stopping = True
            self.capacity_changed.notify_all()
            for session in self.sessions.values():
                self.close_session(session)
            threads = list(self.cleanup_threads)
        for thread in threads:
            thread.join()
        try:
            self.network.close()
            self.network_cleanup_error = None
        except Exception as error:
            self.network_cleanup_error = str(error)
            LOGGER.exception("Could not remove the shared Minecraft network %s", self.network.name)


class RuntimeHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(10)
        try:
            size = struct.unpack("!I", _recv_exact(self.request, 4))[0]
            if not 0 < size <= MAX_REQUEST_BYTES:
                raise RuntimeFault("bad_request", "Invalid request size")
            request = loads(_recv_exact(self.request, size))
            result = self.server.service.dispatch(request)
            response = {"version": PROTOCOL_VERSION, "ok": True, "result": result}
        except RuntimeFault as error:
            response = {"version": PROTOCOL_VERSION, "ok": False,
                        "error": {"code": error.code, "message": str(error)}}
        except (OSError, ValueError, KeyError, TypeError):
            LOGGER.exception("Invalid runtime request")
            response = {"version": PROTOCOL_VERSION, "ok": False,
                        "error": {"code": "bad_request", "message": "Invalid runtime request"}}
        except Exception:
            LOGGER.exception("Runtime operation failed")
            response = {"version": PROTOCOL_VERSION, "ok": False,
                        "error": {"code": "runtime_error", "message": "Minecraft runtime operation failed"}}
        try:
            self.request.sendall(pack(response, MAX_RESPONSE_BYTES))
        except OSError:
            LOGGER.warning("Runtime response receiver disconnected")


class RuntimeServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, path, service):
        self.service = service
        super().__init__(str(path), RuntimeHandler)
        os.chmod(path, 0o660)


class SpectatorHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        LOGGER.debug("Spectator: " + format, *args)

    def send(self, code: int, data: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        service = self.server.service
        path = urlsplit(self.path).path
        if path in {"/", "/sessions.json"}:
            with service.lock:
                values = [{"session_id": key, "task_id": session.task["id"],
                           "closed": session.closed, "view": f"/sessions/{key}/view"}
                          for key, session in service.sessions.items()]
            if path == "/sessions.json":
                self.send(200, json.dumps({"sessions": values}).encode(), "application/json")
            else:
                links = "".join(f'<li><a href="{value["view"]}">{html.escape(value["task_id"])} '
                                f'({value["session_id"][:8]})</a>'
                                f'{" — closed" if value["closed"] else ""}</li>' for value in values)
                page = ("<!doctype html><meta charset='utf-8'><title>Minecraft sessions</title>"
                        "<h1>Minecraft spectators</h1><p>This interface cannot control games.</p><ul>"
                        + links + "</ul><p>Refresh this page to see newly started sessions.</p>")
                self.send(200, page.encode(), "text/html; charset=utf-8")
            return
        parts = path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "sessions":
            self.send(404, b"Not found\n", "text/plain")
            return
        with service.lock:
            session = service.sessions.get(parts[1])
        if session is None:
            self.send(404, b"Unknown session\n", "text/plain")
            return
        if parts[2] == "view":
            page = ("<!doctype html><meta charset='utf-8'><title>Minecraft spectator</title>"
                    "<style>body{margin:0;background:#111;color:#ddd;font-family:sans-serif}"
                    "img{display:block;max-width:100vw;max-height:95vh;object-fit:contain;cursor:none}</style>"
                    "<p>Read-only Minecraft spectator</p><img alt='Minecraft game' src='stream.mjpg'>")
            self.send(200, page.encode(), "text/html; charset=utf-8")
        elif parts[2] == "frame.jpg":
            frame = session.publisher.frame()
            if frame is None:
                self.send(503, b"No frame available\n", "text/plain")
            else:
                self.send(200, frame[0], "image/jpeg")
        elif parts[2] == "stream.mjpg":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            sequence = None
            self.connection.settimeout(5)
            try:
                while not service.stopping:
                    frame = session.publisher.frame()
                    if frame is not None and frame[1] != sequence:
                        jpeg, sequence, timestamp = frame
                        header = (f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n"
                                  f"X-Frame-Sequence: {sequence}\r\nX-Frame-Time: {timestamp}\r\n\r\n").encode()
                        self.wfile.write(header + jpeg + b"\r\n")
                        self.wfile.flush()
                    if session.closed:
                        break
                    session.closed_event.wait(1 / session.settings.capture_fps)
            except (OSError, TimeoutError):
                pass
            self.close_connection = True
        else:
            self.send(404, b"Not found\n", "text/plain")

    def do_POST(self):
        self.close_connection = True
        self.send(405, b"Spectator service only supports GET\n", "text/plain")

    do_PUT = do_DELETE = do_PATCH = do_POST


class SpectatorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service):
        self.service = service
        super().__init__(address, SpectatorHandler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, default=Path.cwd())
    parser.add_argument("--max-sessions", type=int, default=1)
    parser.add_argument("--spectator-host", default="127.0.0.1")
    parser.add_argument("--spectator-port", type=int, default=8766)
    parser.add_argument("--accept-eula", action="store_true")
    args = parser.parse_args(argv)
    if args.spectator_host not in {"127.0.0.1", "localhost"}:
        parser.error("Spectator host must be loopback; use an SSH tunnel for remote access")
    if not 0 <= args.spectator_port <= 65535:
        parser.error("spectator-port must be in 0..65535")
    image_lock = pin_images()
    service = RuntimeService(args.run_root, max_sessions=args.max_sessions,
                             accept_eula=args.accept_eula or os.getenv("EULA", "").lower() == "true")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(service.directory / "runtime.log")])
    # Bind using a relative path: Linux Unix socket addresses have a short
    # length limit, while experiment roots can be arbitrarily long.
    os.chdir(service.directory)
    runtime = spectator = None
    started_servers = []
    stopped = threading.Event()
    previous = {}
    try:
        runtime = RuntimeServer("socket/runtime.sock", service)
        spectator = SpectatorServer((args.spectator_host, args.spectator_port), service)
        service.viewer_base = f"http://127.0.0.1:{spectator.server_port}"
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, lambda *_: stopped.set())
        service.prepare()
        for server in (runtime, spectator):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            started_servers.append(server)
        atomic_json(service.directory / "runtime.json", {"status": "ready", "pid": os.getpid(),
                    "socket_path": str(service.directory / "socket/runtime.sock"),
                    "spectator_url": service.viewer_base, "max_sessions": args.max_sessions,
                    "images": image_lock, "network": service.network.metadata()})
        LOGGER.info("Runtime ready at %s; spectator %s", service.directory / "socket/runtime.sock", service.viewer_base)
        stopped.wait()
    finally:
        for server in (runtime, spectator):
            if server is not None:
                if server in started_servers:
                    server.shutdown()
                server.server_close()
        service.shutdown()
        for signum, previous_handler in previous.items():
            signal.signal(signum, previous_handler)
        cleanup_required = [key for key, session in service.sessions.items() if session.cleanup_failed]
        atomic_json(service.directory / "runtime.json", {"status": "stopped", "pid": os.getpid(),
                                                        "cleanup_required": cleanup_required,
                                                        "images": image_lock,
                                                        "network": service.network.metadata(),
                                                        "network_cleanup_error": service.network_cleanup_error})
    return 1 if cleanup_required or service.network_cleanup_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
