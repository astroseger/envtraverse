"""Own exactly one Docker Compose project; expose pixels and physical input."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from .recording import JsonlTail, atomic_json


class DockerBackend:
    def __init__(self, session_id: str, directory: Path, settings, *, accept_eula: bool = False):
        self.session_id, self.directory, self.settings = session_id, directory, settings
        self.accept_eula = accept_eula
        self.project = "life-minecraft-" + session_id
        self.compose = Path(__file__).resolve().parent / "game" / "compose.yaml"
        self.token = uuid4().hex
        self.url = None
        self.cancelled = threading.Event()
        self.stop_lock = threading.Lock()
        self.stopped = False
        self.telemetry = directory / "telemetry"
        self.input_events = directory / "input-events"
        self.telemetry.mkdir()
        self.input_events.mkdir(mode=0o777)
        self.input_events.chmod(0o777)  # Bridge container runs as uid 1000.
        self.events = JsonlTail(self.telemetry / "events.jsonl")
        self.startup_events = []
        self.inputs = JsonlTail(self.input_events / "events.jsonl")
        self.environment = dict(os.environ, EULA=str(accept_eula).lower(),
            TELEMETRY_DIR=str(self.telemetry), INPUT_EVENTS_DIR=str(self.input_events),
            RUN_ID=session_id, PLAYER_NAME="AgentPlayer", AUTH_MODE="offline", ONLINE_MODE="false",
            DIFFICULTY=settings.difficulty, HARDCORE=str(settings.hardcore).lower(), SEED=str(settings.seed),
            WIDTH=str(settings.width), HEIGHT=str(settings.height), CAPTURE_FPS=str(settings.capture_fps),
            RENDER_FPS=str(settings.render_fps), BRIDGE_TOKEN=self.token)
        self.log = directory / "docker.log"

    def command(self, *arguments: str, timeout: float = 120, capture: bool = False, cancellable: bool = False):
        command = ["docker", "compose", "-f", str(self.compose), "-p", self.project, *arguments]
        if cancellable:
            # Docker's CLI launches the compose plugin as a child. Signal the
            # whole process group, then reap the CLI before issuing compose down.
            with self.log.open("ab") as log:
                process = subprocess.Popen(command, env=self.environment, stdout=log, stderr=subprocess.STDOUT,
                                           start_new_session=True)
                deadline = time.monotonic() + timeout
                try:
                    while process.poll() is None:
                        if self.cancelled.is_set():
                            raise RuntimeError("Minecraft startup was cancelled")
                        if time.monotonic() >= deadline:
                            raise subprocess.TimeoutExpired(command, timeout)
                        self.cancelled.wait(0.1)
                    if process.returncode:
                        raise subprocess.CalledProcessError(process.returncode, command)
                    return process.returncode
                except BaseException:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    # Reap any compose-plugin child that outlived the CLI too.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)
                    raise
        if capture:
            return subprocess.run(command, env=self.environment, check=True, timeout=timeout,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout
        with self.log.open("ab") as log:
            return subprocess.run(command, env=self.environment, check=True, timeout=timeout,
                                  stdout=log, stderr=subprocess.STDOUT)

    def request(self, path: str, data: dict | None = None, *, timeout: float = 2):
        if self.url is None:
            raise RuntimeError("Client bridge is not connected")
        payload = None if data is None else json.dumps(data).encode()
        request = Request(self.url + path, data=payload,
                          headers={"Authorization": "Bearer " + self.token,
                                   "Content-Type": "application/json"})
        with urlopen(request, timeout=timeout) as response:
            body = response.read(16 * 1024 * 1024 + 1)
            if len(body) > 16 * 1024 * 1024:
                raise ValueError("Oversize client response")
            return body, response.headers

    def startup_failure(self, service: str, exit_code=None) -> RuntimeError:
        log = self.directory / ("client-logs/launcher.log" if service == "client" else "containers.log")
        code = "" if exit_code is None else f" (exit code {exit_code})"
        return RuntimeError(f"Minecraft {service} failed during startup{code}; "
                            f"saved log after cleanup: {log}")

    def check_startup_containers(self, containers: list[dict]) -> None:
        """Detect failures even when the bridge died before health could report them."""
        if not containers:
            raise RuntimeError("Minecraft Compose startup did not create any containers")
        inspected = subprocess.run(
            ["docker", "inspect", "--format", "{{json .State}}",
             *(item["container_id"] for item in containers)],
            check=True, capture_output=True, text=True, timeout=5)
        states = [json.loads(line) for line in inspected.stdout.splitlines()]
        if len(states) != len(containers):
            raise RuntimeError("Minecraft container inspection returned incomplete startup state")
        for container, state in zip(containers, states):
            if state.get("Status") in {"exited", "dead", "removing"}:
                raise self.startup_failure(container["service"], state.get("ExitCode"))

    def start(self):
        if not self.accept_eula:
            raise RuntimeError("Minecraft EULA acceptance is required")
        if self.cancelled.is_set():
            raise RuntimeError("Minecraft startup was cancelled")
        deadline = time.monotonic() + self.settings.startup_timeout_seconds
        self.command("up", "-d", "--no-build", "--pull", "never",
                     timeout=self.settings.startup_timeout_seconds, cancellable=True)
        binding = self.command("port", "client", "8000", capture=True).strip().splitlines()[0]
        host, port = binding.rsplit(":", 1)
        if host != "127.0.0.1" or not port.isdigit():
            raise RuntimeError("Client bridge must bind to a dynamic loopback port")
        self.url = f"http://127.0.0.1:{int(port)}"
        containers = self.command("ps", "--all", "--quiet", capture=True).split()
        images = []
        if containers:
            inspection = subprocess.run(["docker", "inspect", *containers], check=True, capture_output=True,
                                        text=True, timeout=15)
            images = [{"service": item.get("Config", {}).get("Labels", {}).get("com.docker.compose.service"),
                       "image_id": item["Image"], "container_id": item["Id"]}
                      for item in json.loads(inspection.stdout)]
        # Private bridge credentials are deliberately not saved in session records.
        atomic_json(self.directory / "backend.json", {"project": self.project,
                    "client_url": self.url, "minecraft_version": "1.21.1",
                    "server_image": os.getenv("MC_SERVER_IMAGE_REFERENCE", os.getenv("MC_SERVER_IMAGE", "life-minecraft-server")),
                    "client_image": os.getenv("MC_CLIENT_IMAGE_REFERENCE", os.getenv("MC_CLIENT_IMAGE", "life-minecraft-client")),
                    "containers": images})
        last_error = None
        next_container_check = 0.0
        while not self.cancelled.is_set() and time.monotonic() < deadline:
            if time.monotonic() >= next_container_check:
                self.check_startup_containers(images)
                next_container_check = time.monotonic() + 2
            try:
                health = json.loads(self.request("/health")[0])
                client = health.get("client", {})
                if client.get("state") in {"exited", "error"}:
                    raise self.startup_failure("client", client.get("exit_code"))
                state, events = self.read_state()
                self.startup_events.extend(events)
                if (health.get("capture_ready") and health.get("client", {}).get("state") == "running"
                        and state.get("connected")
                        and state.get("server_running") and not state.get("telemetry_error")
                        and abs(time.time() - state.get("time_ms", 0) / 1000) < 5):
                    return state, self.frame()
            except (OSError, ValueError, HTTPError, KeyError) as error:
                last_error = error
            self.cancelled.wait(0.2)
        if self.cancelled.is_set():
            raise RuntimeError("Startup was cancelled")
        raise TimeoutError(f"Minecraft startup timed out; inspect {self.directory / 'docker.log'} "
                           f"and {self.directory / 'client-logs/launcher.log'} after cleanup") from last_error

    def frame(self):
        jpeg, headers = self.request("/frame.jpg")
        return jpeg, int(headers["X-Frame-Sequence"]), float(headers["X-Frame-Time"])

    def read_state(self):
        state = json.loads((self.telemetry / "state.json").read_text())
        return state, self.events.read()

    def read_input_events(self):
        return self.inputs.read()

    def action(self, action: dict):
        return json.loads(self.request("/action", action)[0])

    def release(self):
        if self.url is not None:
            return json.loads(self.request("/release", {})[0])

    def stop(self):
        self.cancelled.set()
        with self.stop_lock:
            if self.stopped:
                return
            try:
                self.release()
            except Exception:
                pass
            try:
                logs = self.command("logs", "--no-color", capture=True, timeout=15)
                (self.directory / "containers.log").write_text(logs)
            except Exception:
                pass
            try:
                self.command("cp", "client:/data/client/logs", str(self.directory / "client-logs"), timeout=15)
            except Exception:
                pass
            self.command("down", "--volumes", "--remove-orphans", "--timeout", "20", timeout=90)
            self.stopped = True
