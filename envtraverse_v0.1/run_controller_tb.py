#!/usr/bin/env python3
"""Set up Terminal-Bench, run the controller, then drain the host runtime."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import yaml


ROOT = Path(__file__).resolve().parent
RUNTIME_START_TIMEOUT_SECONDS = 3600
SUPPORTED_BENCHMARKS = ("2.1", "3.0", "4.0", "science-0.1")


def parse_benchmarks(value: str) -> str:
    versions = SUPPORTED_BENCHMARKS if value == "all" else value.split(",")
    if any(version not in SUPPORTED_BENCHMARKS for version in versions):
        raise argparse.ArgumentTypeError(
            "choose 2.1, 3.0, 4.0, science-0.1, a comma-separated combination, or all")
    if len(set(versions)) != len(versions):
        raise argparse.ArgumentTypeError("benchmark selection contains duplicate versions")
    return value


def validate_benchmark_games(config: dict, run_config: Path, benchmarks: str) -> None:
    games = config.get("games")
    if (not isinstance(games, list) or not games
            or any(not isinstance(game, str) or not game.strip() for game in games)):
        raise RuntimeError(f"games in {run_config} must be a non-empty list of non-empty strings")
    versions = SUPPORTED_BENCHMARKS if benchmarks == "all" else benchmarks.split(",")
    prefixes = tuple(f"terminal-bench-{version}-" for version in versions)
    mismatches = [game for game in games
                  if game != "smoke-test-smoke-test" and not game.startswith(prefixes)]
    if mismatches:
        raise RuntimeError(
            f"{run_config} contains games outside --benchmarks {benchmarks}: {', '.join(mismatches)}")


@dataclass
class ShutdownRequest:
    # A signal handler must not acquire a threading.Event's lock: the main
    # thread could have been interrupted while holding that same lock.
    requested: bool = False


class RuntimeOutput:
    """Drain the runtime's output while the controller runs independently."""

    def __init__(self, process: subprocess.Popen[str]):
        self.process = process
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self.read, name="runtime-output", daemon=True)
        self.thread.start()

    def read(self) -> None:
        assert self.process.stdout is not None
        with self.process.stdout as output:
            for line in output:
                print(f"[runtime] {line}", end="", flush=True)
                if " INFO __main__: Runtime ready at " in line:
                    self.ready.set()

    def wait_ready(self, shutdown: ShutdownRequest) -> None:
        deadline = time.monotonic() + RUNTIME_START_TIMEOUT_SECONDS
        while True:
            if shutdown.requested:
                raise KeyboardInterrupt
            if self.process.poll() is not None:
                raise RuntimeError(f"Host runtime exited before startup completed (exit code {self.process.returncode})")
            if self.ready.is_set():
                return
            if not self.thread.is_alive():
                raise RuntimeError("Host runtime output closed before Runtime ready")
            if time.monotonic() >= deadline:
                raise RuntimeError("Timed out waiting for the host runtime to become ready")
            self.ready.wait(0.1)


def start_process(root: Path, command: list[str], **kwargs) -> subprocess.Popen:
    # Terminal Ctrl+C reaches the wrapper. It stops the controller first and
    # signals only the runtime supervisor, allowing its workers to finish.
    return subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL,
                            start_new_session=True, **kwargs)


def wait_process(process: subprocess.Popen, shutdown: ShutdownRequest,
                 *, runtime: subprocess.Popen | None = None) -> int:
    while True:
        if shutdown.requested:
            raise KeyboardInterrupt
        if runtime is not None and runtime.poll() is not None:
            raise RuntimeError(f"Host runtime exited unexpectedly (exit code {runtime.returncode})")
        code = process.poll()
        if code is not None:
            return code if code >= 0 else 128 - code
        time.sleep(0.1)


def stop_process(process: subprocess.Popen, *, startup_group: bool = False) -> int:
    if process.poll() is None:
        try:
            if startup_group:
                # Before the runtime is ready, Bash may still be waiting for
                # pip/git/docker preflight children. No task workers exist yet.
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass
    return process.wait()


def run_experiment(root: Path, run_config: Path, *, benchmarks: str | None = None) -> int:
    for name in ("run", "run_tb"):
        if os.path.lexists(root / name):
            raise RuntimeError(f"Run folder already exists: {root / name}")
    with run_config.open(encoding="utf-8") as source:
        config = yaml.safe_load(source)
    if not isinstance(config, dict) or config.get("environment") != "terminal_bench":
        raise RuntimeError(f"{run_config} must select environment: terminal_bench")
    if benchmarks is not None:
        validate_benchmark_games(config, run_config, benchmarks)

    host = root / "src/server/environments/terminal_bench/host_runtime"
    setup = runtime = controller = None
    output = None
    shutdown = ShutdownRequest()
    previous_handlers = {}
    exit_code = 1

    def handle_signal(_signum: int, _frame: object) -> None:
        shutdown.requested = True

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, handle_signal)
    try:
        print(f"[tb] Setting up the host runtime (benchmarks: {benchmarks or '2.1'}).", flush=True)
        setup_command = ["bash", str(host / "setup.sh")]
        if benchmarks is not None:
            setup_command.extend(["--benchmarks", benchmarks])
        setup = start_process(root, setup_command)
        setup_code = wait_process(setup, shutdown)
        if setup_code != 0:
            raise RuntimeError(f"Host runtime setup failed (exit code {setup_code})")

        print("[tb] Starting the host runtime.", flush=True)
        runtime = start_process(root, ["bash", str(host / "run.sh")], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", bufsize=1)
        output = RuntimeOutput(runtime)
        output.wait_ready(shutdown)

        print("[tb] Starting run_controller.py.", flush=True)
        controller = start_process(root, [sys.executable, "-u", str(root / "run_controller.py"),
                                          "--run-config", str(run_config)])
        exit_code = wait_process(controller, shutdown, runtime=runtime)
    except KeyboardInterrupt:
        print("[tb] Interrupted; stopping the controller before shutting down the runtime.", flush=True)
        exit_code = 130
    finally:
        try:
            if setup is not None and setup.poll() is None:
                stop_process(setup, startup_group=True)
            if controller is not None and controller.poll() is None:
                stop_process(controller)
        finally:
            try:
                if runtime is not None:
                    if runtime.poll() is None:
                        print("[tb] Shutting down the host runtime; waiting for verification and cleanup.", flush=True)
                        ready = output is not None and output.ready.is_set()
                        runtime_code = stop_process(runtime, startup_group=not ready)
                        if runtime_code != 0 and ready and not shutdown.requested:
                            raise RuntimeError(f"Host runtime shutdown failed (exit code {runtime_code})")
                    elif exit_code == 0:
                        raise RuntimeError(f"Host runtime exited unexpectedly (exit code {runtime.returncode})")
            finally:
                if output is not None:
                    output.thread.join()
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
    return 130 if shutdown.requested else exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-config", type=Path, default=ROOT / "run_config.yaml",
                        help="run config to pass to run_controller.py; default: run_config.yaml in the project")
    parser.add_argument("--benchmarks", type=parse_benchmarks, metavar="VERSIONS",
                        help="datasets to prepare: 2.1, 3.0, 4.0, science-0.1, comma-separated, or all; "
                             "default setup: 2.1. Explicit selections must include all configured games")
    args = parser.parse_args(argv)
    try:
        return run_experiment(ROOT, args.run_config.resolve(), benchmarks=args.benchmarks)
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
