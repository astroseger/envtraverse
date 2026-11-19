#!/usr/bin/env python3
"""Build/start the Life Minecraft host runtime, run EnvTraverse, then clean up."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import yaml

from src.controller import run_funs as rf
from src.server.environments.life_minecraft.images import DEFAULT_IMAGES, inspect_images, pin_images

ROOT = rf.ROOT


@dataclass
class ShutdownRequest:
    requested: bool = False


def start_process(root: Path, command: list[str], **kwargs) -> subprocess.Popen:
    return subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL,
                            start_new_session=True, **kwargs)


def wait_process(process, shutdown, *, runtime=None):
    while True:
        if shutdown.requested:
            raise KeyboardInterrupt
        if runtime is not None and runtime.poll() is not None:
            raise RuntimeError(f"Minecraft runtime exited unexpectedly ({runtime.returncode})")
        code = process.poll()
        if code is not None:
            return code if code >= 0 else 128 - code
        time.sleep(.1)


def stop_process(process, *, group=False):
    if process is not None and process.poll() is None:
        try:
            if group:
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass
        # Runtime owns teardown; allow it to collect logs and remove only its games.
        return process.wait()
    return None if process is None else process.returncode


def wait_ready(process, root: Path, shutdown, timeout=60):
    deadline = time.monotonic() + timeout
    path = root / "run_mc/runtime.json"
    while time.monotonic() < deadline:
        if shutdown.requested:
            raise KeyboardInterrupt
        if process.poll() is not None:
            raise RuntimeError(f"Minecraft runtime exited during startup ({process.returncode})")
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            record = {}
        if record.get("status") == "ready":
            return record
        time.sleep(.1)
    raise RuntimeError("Minecraft runtime did not become ready within 60 seconds")


def needs_image_build(*, rebuild=False, skip=False):
    if rebuild and skip:
        raise RuntimeError("Choose either --rebuild-images or --skip-build")
    images = inspect_images()
    unprepared = []
    for service, image in images.items():
        configured = os.environ.get(f"MC_{service.upper()}_IMAGE", DEFAULT_IMAGES[service])
        if configured != DEFAULT_IMAGES[service]:
            if image is None:
                raise RuntimeError(f"Configured custom Minecraft image does not exist: {configured}")
        elif image is None or not image["prepared"]:
            unprepared.append(service)
    if skip and unprepared:
        raise RuntimeError("Minecraft images are missing or lack prepared assets; run without --skip-build once")
    return rebuild or bool(unprepared)


def run_experiment(source_root: Path, run_config: Path, *, paths=None,
                   accept_eula=False, skip_build=False, rebuild_images=False,
                   spectator_port=8766):
    paths = paths or rf.RunPaths()
    for directory in (paths.run_dir, paths.run_mc_dir):
        if os.path.lexists(directory):
            raise RuntimeError(f"Run folder already exists: {directory}")
    with run_config.open(encoding="utf-8") as source:
        config = yaml.safe_load(source)
    if not isinstance(config, dict) or config.get("environment") != "life_minecraft":
        raise RuntimeError("The run configuration must select environment: life_minecraft")
    if not accept_eula and os.environ.get("EULA", "").lower() != "true":
        raise RuntimeError("Minecraft requires EULA acceptance; use --accept-eula after accepting https://www.minecraft.net/eula")
    if type(spectator_port) is not int or not 0 <= spectator_port <= 65535:
        raise RuntimeError("spectator_port must be between 0 and 65535")

    host = source_root / "src/server/environments/life_minecraft/host_runtime"
    setup = runtime = controller = output_thread = None
    shutdown = ShutdownRequest()
    previous = {}

    def request_stop(*_):
        shutdown.requested = True

    def print_output(process):
        assert process.stdout is not None
        with process.stdout:
            for line in process.stdout:
                print("[mc-runtime] " + line, end="", flush=True)

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, request_stop)
    try:
        if needs_image_build(rebuild=rebuild_images, skip=skip_build):
            print("[mc] Preparing Minecraft images and reusable assets. Later runs reuse these images.", flush=True)
            setup = start_process(source_root, ["bash", str(host / "setup.sh")])
            code = wait_process(setup, shutdown)
            if code:
                raise RuntimeError(f"Minecraft image setup failed ({code})")
        else:
            print("[mc] Reusing prepared Minecraft images; no build or asset installation.", flush=True)
        runtime_environment = dict(os.environ)
        resolved = pin_images(runtime_environment)
        for service, image in resolved.items():
            print(f"[mc] Frozen {service} image: {image['id']}", flush=True)
        command = ["bash", str(host / "run.sh"), "--run-root", str(paths.root),
                   "--spectator-port", str(spectator_port)]
        if accept_eula:
            command.append("--accept-eula")
        runtime = start_process(source_root, command, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", bufsize=1, env=runtime_environment)
        output_thread = threading.Thread(target=print_output, args=(runtime,), daemon=True)
        output_thread.start()
        ready = wait_ready(runtime, paths.root, shutdown)
        print(f"[mc] Spectator: {ready.get('spectator_url', 'see run_mc/runtime.json')}", flush=True)
        command = [sys.executable, "-u", str(source_root / "run_controller.py"),
                   "--run-root", str(paths.root), "--run-config", str(run_config)]
        if paths.accounts_root is not None:
            command += ["--accounts-dir", str(paths.accounts_root)]
        # Keep the ordinary controller's stdin (e.g. API-backend cost confirmation).
        controller = subprocess.Popen(command, cwd=source_root, start_new_session=True)
        code = wait_process(controller, shutdown, runtime=runtime)
    except KeyboardInterrupt:
        code = 130
    finally:
        try:
            stop_process(setup, group=True)
            stop_process(controller)
        finally:
            try:
                runtime_code = stop_process(runtime)
                if runtime_code not in (None, 0) and not shutdown.requested:
                    raise RuntimeError(f"Minecraft runtime shutdown failed ({runtime_code}); inspect run_mc")
            finally:
                if output_thread is not None:
                    output_thread.join(timeout=5)
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
    return 130 if shutdown.requested else code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    rf.add_run_path_arguments(parser)
    parser.add_argument("--accept-eula", action="store_true", help="Accept https://www.minecraft.net/eula for this installation")
    build = parser.add_mutually_exclusive_group()
    build.add_argument("--skip-build", action="store_true", help="Require existing prepared images; normally reused automatically")
    build.add_argument("--rebuild-images", action="store_true", help="Rebuild the Minecraft images; reusable asset/dependency layers stay cached")
    parser.add_argument("--spectator-port", type=int, default=8766, help="Loopback viewer port; 0 chooses a free port")
    args = parser.parse_args(argv)
    try:
        paths = rf.RunPaths(args.run_root, args.accounts_dir)
        return run_experiment(ROOT, paths.resolve_config(args.run_config), paths=paths,
                              accept_eula=args.accept_eula, skip_build=args.skip_build,
                              rebuild_images=args.rebuild_images,
                              spectator_port=args.spectator_port)
    except (OSError, RuntimeError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
