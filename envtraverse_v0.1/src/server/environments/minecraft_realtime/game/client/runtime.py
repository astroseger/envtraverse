"""Launch the actual client once; keep the observation API alive after it exits.

PortableMC 5.0.6 supplies Minecraft installation, Quick Play, and Microsoft
device-code authentication. Authentication data is never baked into the image.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
import uuid

from .install_assets import cached_assets_ready
from .options import configured_render_fps


DATA = Path(os.environ.get("CLIENT_DATA_DIR", "/data/client"))
STATUS = Path(os.environ.get("CLIENT_STATUS_PATH", "/run/client/status.json"))
ASSET_INSTALL_ATTEMPTS = 3


def dimensions() -> tuple[int, int]:
    width, height = int(os.getenv("WIDTH", "960")), int(os.getenv("HEIGHT", "540"))
    if not (320 <= width <= 3840 and 240 <= height <= 2160):
        raise ValueError("WIDTH must be 320..3840 and HEIGHT 240..2160")
    return width, height


def launcher_base() -> list[str]:
    return ["portablemc", "--main-dir", str(DATA / "minecraft"),
            "--msa-db-file", str(DATA / "auth" / "portablemc_msa.json")]


def launch_command() -> list[str]:
    width, height = dimensions()
    player = os.getenv("PLAYER_NAME", "AgentPlayer")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", player):
        raise ValueError("PLAYER_NAME must be a valid Java Edition profile name")
    mode = os.getenv("AUTH_MODE", "offline")
    if mode not in {"offline", "microsoft"}:
        raise ValueError("AUTH_MODE must be offline or microsoft")
    # Pin the protocol/game version alongside the server observer.
    version = os.getenv("MINECRAFT_VERSION", "1.21.1")
    if version != "1.21.1":
        raise ValueError("This runtime and observer are validated for Minecraft 1.21.1")
    port = int(os.getenv("SERVER_PORT", "25565"))
    if not 1 <= port <= 65535:
        raise ValueError("SERVER_PORT is out of range")
    args = launcher_base() + [
        "start", version, "--mc-dir", str(DATA / "minecraft"),
        "--jvm", "/opt/java/openjdk/bin/java",
        "--jvm-arg=-Xms512M,-Xmx" + os.getenv("CLIENT_MEMORY", "2G"),
        "--resolution", f"{width}x{height}",
        "--join-server", os.getenv("SERVER_HOST", "server"),
        "--join-server-port", str(port), "--username", player,
    ]
    if mode == "microsoft":
        args.append("--auth")
    return args


def configure_options() -> None:
    """Keep mouse/capture semantics fixed without destroying unrelated options."""
    width, height = dimensions()
    path = DATA / "minecraft" / "options.txt"
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            key, separator, value = line.partition(":")
            if separator:
                values[key] = value
    values.update({
        "pauseOnLostFocus": "false", "rawMouseInput": "false",
        "fullscreen": "false", "overrideWidth": str(width),
        "overrideHeight": str(height), "renderDistance": "6",
        "simulationDistance": "6", "maxFps": str(configured_render_fps()),
        "enableVsync": "false", "graphicsMode": "0", "cloudStatus": "0",
        "particles": "2", "tutorialStep": "none", "onboardAccessibility": "false",
        "skipMultiplayerWarning": "true", "autoJump": "false",
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{key}:{value}\n" for key, value in values.items()))


class Runtime:
    def __init__(self) -> None:
        self.session_id = str(uuid.uuid4())
        self.stopping = False
        self.children: list[subprocess.Popen] = []
        self.log_files = []

    def status(self, state: str, **fields) -> None:
        STATUS.parent.mkdir(parents=True, exist_ok=True)
        temporary = STATUS.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "state": state, "session_id": self.session_id,
            "timestamp": time.time(), "player_name": os.getenv("PLAYER_NAME", "AgentPlayer"),
            **fields,
        }))
        temporary.replace(STATUS)

    def spawn(self, argv: list[str], log_name: str | None = None) -> subprocess.Popen:
        target = None
        if log_name:
            target = (DATA / "logs" / log_name).open("ab", buffering=0)
            self.log_files.append(target)
        child = subprocess.Popen(argv, stdout=target, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        self.children.append(child)
        return child

    def stop(self, *_args) -> None:
        self.stopping = True

    def wait_server(self) -> None:
        host, port = os.getenv("SERVER_HOST", "server"), int(os.getenv("SERVER_PORT", "25565"))
        deadline = time.monotonic() + int(os.getenv("SERVER_WAIT_SECONDS", "300"))
        while time.monotonic() < deadline and not self.stopping:
            try:
                with socket.create_connection((host, port), timeout=2):
                    return
            except OSError:
                time.sleep(1)
        raise RuntimeError(f"Server {host}:{port} did not become reachable")

    @staticmethod
    def check_install_dependencies(dependencies: tuple[subprocess.Popen, ...]) -> None:
        if any(child.poll() is not None for child in dependencies):
            raise RuntimeError("A display or observation process exited during asset preparation")

    def wait_install_retry(self, seconds: float, dependencies: tuple[subprocess.Popen, ...]) -> bool:
        """Backoff is interruptible; it never delays shutdown for a whole retry interval."""
        deadline = time.monotonic() + seconds
        while not self.stopping:
            self.check_install_dependencies(dependencies)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(0.1, remaining))
        return False

    def prepare_assets(self, command: list[str], dependencies: tuple[subprocess.Popen, ...]) -> bool:
        """Retry downloads before launching Java; never retry a running game.

        PortableMC's dry mode installs the pinned version without starting the
        client. Authentication belongs to the subsequent single game launch.
        The host's startup deadline still bounds all attempts together.
        """
        if self.stopping:
            return False
        self.check_install_dependencies(dependencies)
        if cached_assets_ready(DATA / "minecraft"):
            self.status("starting", phase="assets_ready", source="image")
            print("Using prepared Minecraft assets from the client image; no installation needed.", flush=True)
            return True
        install = [argument for argument in command if argument != "--auth"] + ["--dry"]
        log_path = DATA / "logs" / "launcher.log"
        for attempt in range(1, ASSET_INSTALL_ATTEMPTS + 1):
            if self.stopping:
                return False
            self.check_install_dependencies(dependencies)
            self.status("starting", phase="installing", attempt=attempt,
                        max_attempts=ASSET_INSTALL_ATTEMPTS)
            marker = f"Preparing Minecraft assets (attempt {attempt}/{ASSET_INSTALL_ATTEMPTS})"
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"\n[minecraft_realtime] {marker}\n")
            print(f"{marker}; details: {log_path}", flush=True)
            installer = self.spawn(install, "launcher.log")
            while installer.poll() is None:
                if self.stopping:
                    return False  # main's cleanup terminates the installer process group.
                self.check_install_dependencies(dependencies)
                time.sleep(0.1)
            if self.stopping:
                return False
            self.check_install_dependencies(dependencies)
            if installer.returncode == 0:
                return True
            print(f"Minecraft asset preparation attempt {attempt} failed "
                  f"(exit code {installer.returncode}); details: {log_path}", flush=True)
            if attempt == ASSET_INSTALL_ATTEMPTS:
                raise RuntimeError(f"Minecraft asset preparation failed after {attempt} attempts "
                                   f"(exit code {installer.returncode}); inspect {log_path}")
            if not self.wait_install_retry(float(attempt), dependencies):
                return False
        return False

    def run(self) -> int:
        width, height = dimensions()
        command = launch_command()  # Validate config before starting any children.
        configure_options()
        self.status("starting", phase="display")
        xvfb = self.spawn(["Xvfb", os.getenv("DISPLAY", ":99"), "-screen", "0",
                           f"{width}x{height}x24", "-nolisten", "tcp", "-ac",
                           "+extension", "GLX", "+extension", "RANDR"], "xvfb.log")
        for _ in range(100):
            if xvfb.poll() is not None:
                raise RuntimeError("Xvfb failed; inspect client/logs/xvfb.log")
            check = subprocess.run(["xdpyinfo"], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
            if check.returncode == 0:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Virtual display did not become ready")
        wm = self.spawn(["openbox", "--config-file", "/app/client/openbox.xml"], "openbox.log")
        api = self.spawn([sys.executable, "-m", "uvicorn", "bridge.app:app",
                          "--host", "0.0.0.0", "--port", "8000", "--no-access-log"])
        self.status("starting", phase="waiting_for_server")
        self.wait_server()
        if not self.prepare_assets(command, (xvfb, wm, api)):
            self.status("stopped")
            return 0
        self.status("starting", phase="launching")
        print("Launching Minecraft; launcher output is in /data/client/logs/launcher.log", flush=True)
        # The preceding installation populated the exact pinned version. Avoid
        # refreshing Mojang's manifest between successful installation and play.
        game = self.spawn(command + ["--fetch-exclude-all"], "launcher.log")
        window_found = False
        exit_recorded = False
        while not self.stopping:
            if xvfb.poll() is not None or wm.poll() is not None or api.poll() is not None:
                raise RuntimeError("A display or observation process exited")
            code = game.poll()
            if code is not None and not exit_recorded:
                # No reconnect or automatic restart: departure is an evaluator event.
                self.status("exited", exit_code=code)
                print(f"Minecraft/launcher exited ({code}); observation API remains available", flush=True)
                exit_recorded = True
            elif code is None and not window_found:
                found = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", "Minecraft"],
                                       capture_output=True, text=True)
                if found.returncode == 0 and found.stdout.strip():
                    window_id = found.stdout.splitlines()[0]
                    subprocess.run(["xdotool", "windowmove", window_id, "0", "0",
                                    "windowsize", window_id, str(width), str(height),
                                    "windowactivate", "--sync", window_id], check=False)
                    self.status("running", pid=game.pid, window_id=window_id)
                    window_found = True
            time.sleep(0.5)
        self.status("stopped")
        return 0

    def cleanup(self) -> None:
        for child in reversed(self.children):
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 10
        for child in reversed(self.children):
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
        for log_file in self.log_files:
            log_file.close()


def main() -> int:
    os.umask(0o077)
    (DATA / "logs").mkdir(parents=True, exist_ok=True)
    (DATA / "auth").mkdir(parents=True, exist_ok=True)
    (DATA / "minecraft").mkdir(parents=True, exist_ok=True)
    if len(sys.argv) > 1:
        if sys.argv[1] == "auth":
            # For example: docker compose run --rm --no-deps client auth login --no-browser
            os.execvp("portablemc", launcher_base() + sys.argv[1:])
        if sys.argv[1:] == ["check"]:
            try:
                configured_render_fps()
                config = {"command": launch_command(), "resolution": dimensions()}
            except ValueError as exc:
                raise SystemExit(str(exc)) from None
            print(json.dumps(config))
            return 0
        raise SystemExit("Usage: client.runtime [auth <login|list|refresh|forget> [options] | check]")
    runtime = Runtime()
    signal.signal(signal.SIGTERM, runtime.stop)
    signal.signal(signal.SIGINT, runtime.stop)
    try:
        return runtime.run()
    except Exception as exc:
        runtime.status("error", error=str(exc))
        print(f"Client runtime failed: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        runtime.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
