"""Exercise the wrapper with real subprocesses/signals and simulated workloads."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import yaml


SCRIPT = Path(__file__).resolve().parents[1] / "run_controller_tb.py"
FAKE_PROCESSES = r'''
import json, os, signal, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OPTIONS = json.loads((ROOT / 'scenario.json').read_text())

def event(name, **data):
    with (ROOT / 'events.jsonl').open('a') as output:
        output.write(json.dumps({'event': name, **data}) + '\n')
    (ROOT / name).touch()

def wait_for(name):
    while not (ROOT / name).exists():
        time.sleep(.01)

def run_root():
    return Path(sys.argv[sys.argv.index('--run-root') + 1]) if '--run-root' in sys.argv else ROOT

def setup():
    (ROOT / 'setup.pid').write_text(str(os.getpid()))
    if OPTIONS.get('block_setup'):
        stopped = False
        def stop(*_):
            nonlocal stopped
            stopped = True
        signal.signal(signal.SIGTERM, stop)
        child = subprocess.Popen([sys.executable, '-u', __file__, 'setup_child'])
        wait_for('setup_child_started')
        event('setup_started', args=sys.argv[2:], cwd=os.getcwd())
        while not stopped:
            time.sleep(.01)
        child.wait(timeout=5)
        event('setup_stopped')
        return 0
    event('setup_started', args=sys.argv[2:], cwd=os.getcwd())
    print('setup output', flush=True)
    return OPTIONS.get('setup_exit', 0)

def setup_child():
    (ROOT / 'setup_child.pid').write_text(str(os.getpid()))
    def stop(*_):
        event('setup_child_stopped')
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    event('setup_child_started')
    while True:
        time.sleep(.01)

def worker():
    (ROOT / 'worker.pid').write_text(str(os.getpid()))
    def stop(*_):
        event('worker_interrupted')
        raise SystemExit(5)
    signal.signal(signal.SIGINT, stop)
    event('worker_started')
    wait_for('release_worker')
    event('worker_completed')
    return 0

def runtime():
    (ROOT / 'runtime.pid').write_text(str(os.getpid()))
    stopped = False
    def stop(signum, _frame):
        nonlocal stopped
        stopped = True
        event('runtime_stop_requested', signum=signum)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    (run_root() / 'run_tb').mkdir(parents=True)
    event('runtime_started', args=sys.argv[2:], cwd=os.getcwd())
    if 'runtime_start_exit' in OPTIONS:
        return OPTIONS['runtime_start_exit']
    while OPTIONS.get('block_ready') and not (ROOT / 'release_ready').exists():
        if stopped:
            event('runtime_completed')
            return 0
        time.sleep(.01)
    child = subprocess.Popen([sys.executable, '-u', __file__, 'worker'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_for('worker_started')
    event('runtime_ready')
    print('2026-09-24 12:00:00 INFO __main__: Runtime ready at socket/runtime.sock (tasks are prepared only on start)', flush=True)
    print('runtime stderr output', file=sys.stderr, flush=True)
    for number in range(OPTIONS.get('output_lines', 0)):
        print('runtime output %s: %s' % (number, 'x' * 1024), flush=True)
    if 'runtime_early_exit' in OPTIONS:
        wait_for('controller_started')
        (ROOT / 'release_worker').touch()
        child.wait(timeout=5)
        event('runtime_completed')
        return OPTIONS['runtime_early_exit']
    while not stopped:
        time.sleep(.01)
    event('runtime_draining')
    if OPTIONS.get('hold_validation'):
        wait_for('release_validation')
    (ROOT / 'release_worker').touch()
    child.wait(timeout=5)
    if not OPTIONS.get('no_summary'):
        print('2026-09-24 12:00:01 INFO __main__: 1 tasks validated. Errors: 0. Aborted: 0. Active: 0. No active sessions.', flush=True)
    event('runtime_completed')
    return OPTIONS.get('runtime_shutdown_exit', 0)

def controller():
    (ROOT / 'controller.pid').write_text(str(os.getpid()))
    stopped = False
    def stop(signum, _frame):
        nonlocal stopped
        stopped = True
        event('controller_stop_requested', signum=signum)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    assert (ROOT / 'runtime_ready').exists()
    (run_root() / 'run').mkdir()
    event('controller_started', args=sys.argv[1:], cwd=os.getcwd())
    print('controller output', flush=True)
    if OPTIONS.get('block_controller'):
        while not stopped:
            time.sleep(.01)
    event('controller_completed')
    return 130 if stopped else OPTIONS.get('controller_exit', 0)

if __name__ == '__main__':
    raise SystemExit(globals()[sys.argv[1]]())
'''


class TerminalBenchControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tb-wrapper-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project with spaces"
        self.root.mkdir()
        self.outside = Path(self.temporary.name) / "caller"
        self.outside.mkdir()
        self.host = self.root / "src/server/environments/terminal_bench/host_runtime"
        self.host.mkdir(parents=True)
        shutil.copyfile(SCRIPT, self.root / SCRIPT.name)
        for name in ("run_funs.py", "agent_api.py"):
            shutil.copyfile(SCRIPT.parent / name, self.root / name)
        helper = self.root / "fake_processes.py"
        helper.write_text(FAKE_PROCESSES)
        for script, mode in (("setup.sh", "setup"), ("run.sh", "runtime")):
            (self.host / script).write_text(
                "#!/usr/bin/env bash\nexec " + shlex.join([sys.executable, "-u", str(helper), mode]) + ' "$@"\n')
        (self.root / "run_controller.py").write_text("from fake_processes import controller\nraise SystemExit(controller())\n")
        (self.root / "run_config.yaml").write_text("environment: terminal_bench\n")
        self.output_path = self.root / "output.log"
        self.processes = []
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        for name in ("release_ready", "release_validation", "release_worker"):
            (self.root / name).touch()
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                    # Only the test doubles record these PIDs.
                    for path in self.root.glob("*.pid"):
                        try:
                            os.kill(int(path.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def start(self, *, options=None, arguments=(), startup_timeout=None):
        (self.root / "scenario.json").write_text(json.dumps(options or {}))
        if startup_timeout is None:
            command = [sys.executable, str(self.root / SCRIPT.name)]
        else:
            code = (f"import sys; sys.path.insert(0, {str(self.root)!r}); "
                    f"import run_controller_tb as runner; runner.RUNTIME_START_TIMEOUT_SECONDS = {startup_timeout}; "
                    "raise SystemExit(runner.main())")
            command = [sys.executable, "-c", code]
        with self.output_path.open("w") as output:
            process = subprocess.Popen([*command, *arguments], cwd=self.outside, stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True,
                                       env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1"})
        self.processes.append(process)
        return process

    def output(self):
        return self.output_path.read_text()

    def events(self):
        path = self.root / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wait_marker(self, name, process):
        deadline = time.monotonic() + 10
        while not (self.root / name).exists():
            if process.poll() is not None or time.monotonic() > deadline:
                self.fail(f"Did not reach {name}:\n{self.output()}")
            time.sleep(.01)

    def finish(self, process, expected=0):
        self.assertEqual(process.wait(timeout=10), expected, self.output())

    def assert_order(self, *names):
        recorded = [event["event"] for event in self.events()]
        indices = [recorded.index(name) for name in names]
        self.assertEqual(indices, sorted(indices), recorded)

    def test_waits_for_readiness_streams_output_and_drains_verification(self):
        config = self.outside / "chosen config.yaml"
        config.write_text("environment: terminal_bench\n")
        process = self.start(options={"block_ready": True, "hold_validation": True, "output_lines": 150},
                             arguments=["--run-config", config.name])
        self.wait_marker("runtime_started", process)
        self.assertFalse((self.root / "controller_started").exists())
        (self.root / "release_ready").touch()
        self.wait_marker("runtime_draining", process)
        self.assertIsNone(process.poll())
        self.assertTrue((self.root / "controller_completed").exists())
        self.assertIn("[runtime] runtime output 0:", self.output())
        self.assertIn("[runtime] runtime stderr output", self.output())
        self.assertIn("controller output", self.output())
        (self.root / "release_validation").touch()
        self.finish(process)
        self.assert_order("setup_started", "runtime_ready", "controller_started", "controller_completed",
                          "runtime_stop_requested", "worker_completed", "runtime_completed")
        controller = next(event for event in self.events() if event["event"] == "controller_started")
        self.assertEqual(controller["args"], ["--run-root", str(self.root), "--run-config", str(config)])
        setup = next(event for event in self.events() if event["event"] == "setup_started")
        self.assertEqual(setup["args"], [])
        self.assertTrue(all(event["cwd"] == str(self.root) for event in self.events() if "cwd" in event))
        self.assertFalse((self.root / "worker_interrupted").exists())
        self.assertIn("[runtime] runtime output 149:", self.output())
        self.assertIn("Active: 0. No active sessions.", self.output())

    def test_no_completion_summary_does_not_hang(self):
        process = self.start(options={"no_summary": True})
        self.finish(process)
        self.assertNotIn("No active sessions", self.output())
        self.assertTrue((self.root / "runtime_completed").exists())

    def test_external_relative_run_root_uses_its_default_config_and_source_scripts(self):
        experiment = self.outside / "experiment with spaces"
        experiment.mkdir()
        config = experiment / "run_config.yaml"
        config.write_text("environment: terminal_bench\ngames: [terminal-bench-4.0-example]\n")
        # Existing outputs in the checkout must not block an external experiment.
        for name in ("run", "run_tb"):
            (self.root / name).mkdir()
            (self.root / name / "keep.txt").write_text("existing run")
        process = self.start(arguments=["--run-root", experiment.name, "--benchmarks", "4.0"])
        self.finish(process)
        self.assertTrue((experiment / "run").is_dir())
        self.assertTrue((experiment / "run_tb").is_dir())
        for name in ("run", "run_tb"):
            self.assertEqual((self.root / name / "keep.txt").read_text(), "existing run")
        setup, runtime, controller = (next(event for event in self.events() if event["event"] == name)
                                      for name in ("setup_started", "runtime_started", "controller_started"))
        self.assertEqual(setup["args"], ["--benchmarks", "4.0"])
        self.assertEqual(runtime["args"], ["--run-root", str(experiment)])
        self.assertEqual(controller["args"], ["--run-root", str(experiment), "--run-config", str(config)])
        self.assertTrue(all(event["cwd"] == str(self.root) for event in (setup, runtime, controller)))

    def test_external_run_root_keeps_explicit_relative_config_relative_to_caller(self):
        experiment = self.outside / "experiment"
        experiment.mkdir()
        (experiment / "run_config.yaml").write_text("environment: wrong\n")
        config = self.outside / "explicit.yaml"
        config.write_text("environment: terminal_bench\n")
        process = self.start(arguments=["--run-root", experiment.name, "--run-config", config.name])
        self.finish(process)
        controller = next(event for event in self.events() if event["event"] == "controller_started")
        self.assertEqual(controller["args"], ["--run-root", str(experiment), "--run-config", str(config)])

    def test_external_run_root_rejects_existing_outputs_before_setup(self):
        experiment = self.outside / "experiment"
        experiment.mkdir()
        (experiment / "run_config.yaml").write_text("environment: terminal_bench\n")
        for name in ("run", "run_tb"):
            with self.subTest(name=name):
                path = experiment / name
                path.mkdir()
                process = self.start(arguments=["--run-root", str(experiment)])
                self.finish(process, 1)
                self.assertIn(str(path), self.output())
                self.assertEqual(self.events(), [])
                path.rmdir()

    def test_controller_failure_is_preserved_and_runtime_is_drained(self):
        process = self.start(options={"controller_exit": 7, "no_summary": True})
        self.finish(process, 7)
        self.assert_order("controller_completed", "runtime_stop_requested", "worker_completed", "runtime_completed")

    def test_ctrl_c_stops_controller_before_runtime_and_waits_for_workers(self):
        process = self.start(options={"block_controller": True, "hold_validation": True})
        self.wait_marker("controller_started", process)
        # Simulate the terminal sending Ctrl+C to the foreground process group.
        os.killpg(process.pid, signal.SIGINT)
        self.wait_marker("runtime_draining", process)
        self.assertIsNone(process.poll())
        self.assert_order("controller_stop_requested", "controller_completed", "runtime_stop_requested")
        process.send_signal(signal.SIGINT)
        self.assertFalse((self.root / "worker_interrupted").exists())
        (self.root / "release_validation").touch()
        self.finish(process, 130)
        self.assertTrue((self.root / "worker_completed").exists())

    def test_sigterm_also_cleans_up(self):
        process = self.start(options={"block_controller": True})
        self.wait_marker("controller_started", process)
        process.terminate()
        self.finish(process, 130)
        self.assert_order("controller_completed", "runtime_stop_requested", "runtime_completed")

    def test_setup_failure_does_not_start_runtime(self):
        process = self.start(options={"setup_exit": 9})
        self.finish(process, 1)
        self.assertIn("setup failed (exit code 9)", self.output())
        self.assertFalse((self.root / "runtime_started").exists())

    def test_interrupt_during_setup_stops_its_children(self):
        process = self.start(options={"block_setup": True})
        self.wait_marker("setup_started", process)
        process.send_signal(signal.SIGINT)
        self.finish(process, 130)
        self.assertTrue((self.root / "setup_child_stopped").exists())
        self.assertTrue((self.root / "setup_stopped").exists())
        self.assertFalse((self.root / "runtime_started").exists())

    def test_runtime_startup_failure_does_not_start_controller(self):
        process = self.start(options={"runtime_start_exit": 4})
        self.finish(process, 1)
        self.assertIn("Host runtime exited before startup completed (exit code 4)", self.output())
        self.assertFalse((self.root / "controller_started").exists())

    def test_runtime_readiness_timeout_stops_startup(self):
        process = self.start(options={"block_ready": True}, startup_timeout=.5)
        self.finish(process, 1)
        self.assertIn("Timed out waiting", self.output())
        self.assertTrue((self.root / "runtime_completed").exists())
        self.assertFalse((self.root / "controller_started").exists())

    def test_interrupt_before_runtime_ready_cleans_up(self):
        process = self.start(options={"block_ready": True})
        self.wait_marker("runtime_started", process)
        process.send_signal(signal.SIGINT)
        self.finish(process, 130)
        self.assertTrue((self.root / "runtime_completed").exists())
        self.assertFalse((self.root / "controller_started").exists())

    def test_unexpected_runtime_exit_stops_controller_even_with_zero_exit_code(self):
        process = self.start(options={"runtime_early_exit": 0, "block_controller": True})
        self.finish(process, 1)
        self.assertIn("Host runtime exited unexpectedly (exit code 0)", self.output())
        self.assertTrue((self.root / "controller_completed").exists())

    def test_runtime_shutdown_failure_is_reported(self):
        process = self.start(options={"runtime_shutdown_exit": 6, "controller_exit": 7})
        self.finish(process, 1)
        self.assertIn("Host runtime shutdown failed (exit code 6)", self.output())

    def test_existing_paths_including_dangling_symlinks_reject_before_setup(self):
        for name in ("run", "run_tb"):
            for kind in ("directory", "file", "symlink"):
                with self.subTest(name=name, kind=kind):
                    path = self.root / name
                    if kind == "directory":
                        path.mkdir()
                    elif kind == "file":
                        path.touch()
                    else:
                        path.symlink_to(self.root / "missing")
                    process = self.start()
                    self.finish(process, 1)
                    self.assertIn("Run folder already exists", self.output())
                    self.assertEqual(self.events(), [])
                    if kind == "directory":
                        path.rmdir()
                    else:
                        path.unlink()

    def test_wrong_environment_is_rejected_before_setup(self):
        (self.root / "run_config.yaml").write_text("environment: arc3\n")
        process = self.start()
        self.finish(process, 1)
        self.assertIn("must select environment: terminal_bench", self.output())
        self.assertEqual(self.events(), [])

    def test_science_selection_forwards_setup_and_full_run_config(self):
        source = SCRIPT.parent / "run_config_terminal_bench_science_0.1.yaml"
        config = self.outside / "science config.yaml"
        shutil.copyfile(source, config)
        games = yaml.safe_load(config.read_text())["games"]
        self.assertEqual(len(games), 70)
        self.assertEqual(len(set(games)), 70)
        process = self.start(arguments=["--benchmarks", "science-0.1", "--run-config", config.name])
        self.finish(process)
        setup = next(event for event in self.events() if event["event"] == "setup_started")
        self.assertEqual(setup["args"], ["--benchmarks", "science-0.1"])
        controller = next(event for event in self.events() if event["event"] == "controller_started")
        self.assertEqual(controller["args"], ["--run-root", str(self.root), "--run-config", str(config)])
        self.assertEqual(config.read_text(), source.read_text())

    def test_combined_selection_accepts_mixed_games(self):
        (self.root / "run_config.yaml").write_text(json.dumps({
            "environment": "terminal_bench", "games": [
                "terminal-bench-3.0-task", "terminal-bench-4.0-task",
                "terminal-bench-science-0.1-domain/field/task", "smoke-test-smoke-test"]}))
        selection = "3.0,4.0,science-0.1"
        process = self.start(arguments=["--benchmarks", selection])
        self.finish(process)
        setup = next(event for event in self.events() if event["event"] == "setup_started")
        self.assertEqual(setup["args"], ["--benchmarks", selection])

    def test_all_selection_accepts_every_benchmark(self):
        (self.root / "run_config.yaml").write_text(json.dumps({
            "environment": "terminal_bench", "games": [
                "terminal-bench-2.1-task", "terminal-bench-3.0-task", "terminal-bench-4.0-task",
                "terminal-bench-science-0.1-domain/field/task", "smoke-test-smoke-test"]}))
        process = self.start(arguments=["--benchmarks", "all"])
        self.finish(process)
        setup = next(event for event in self.events() if event["event"] == "setup_started")
        self.assertEqual(setup["args"], ["--benchmarks", "all"])

    def test_smoke_only_config_is_allowed_with_science_selection(self):
        (self.root / "run_config.yaml").write_text(
            "environment: terminal_bench\ngames: [smoke-test-smoke-test]\n")
        self.finish(self.start(arguments=["--benchmarks", "science-0.1"]))

    def test_omitted_selection_preserves_use_of_preinstalled_datasets(self):
        (self.root / "run_config.yaml").write_text(
            "environment: terminal_bench\ngames: [terminal-bench-science-0.1-domain/field/task]\n")
        self.finish(self.start())
        setup = next(event for event in self.events() if event["event"] == "setup_started")
        self.assertEqual(setup["args"], [])

    def test_games_outside_explicit_selection_fail_before_setup(self):
        for game in ["terminal-bench-2.1-task", "terminal-bench-science-0.10-domain/field/task", "unknown"]:
            with self.subTest(game=game):
                (self.root / "run_config.yaml").write_text(json.dumps({
                    "environment": "terminal_bench", "games": [
                        "terminal-bench-science-0.1-domain/field/task", game]}))
                process = self.start(arguments=["--benchmarks", "science-0.1"])
                self.finish(process, 1)
                self.assertIn("outside --benchmarks science-0.1", self.output())
                self.assertIn(game, self.output())
                self.assertEqual(self.events(), [])

    def test_invalid_selections_fail_before_setup(self):
        for selection in ["", "5.0", "science", "all,3.0", "3.0,3.0", "2.1,,3.0",
                          ",2.1", "science-0.1,", " science-0.1"]:
            with self.subTest(selection=selection):
                process = self.start(arguments=["--benchmarks", selection])
                self.finish(process, 2)
                self.assertIn("--benchmarks", self.output())
                self.assertEqual(self.events(), [])
        self.finish(self.start(arguments=["--benchmarks"]), 2)
        self.assertEqual(self.events(), [])

    def test_explicit_selection_requires_a_valid_games_list(self):
        for games in [None, [], "terminal-bench-2.1-task", [42], [" "]]:
            with self.subTest(games=games):
                (self.root / "run_config.yaml").write_text(json.dumps({
                    "environment": "terminal_bench", "games": games}))
                self.finish(self.start(arguments=["--benchmarks", "2.1"]), 1)
                self.assertIn("non-empty list of non-empty strings", self.output())
                self.assertEqual(self.events(), [])

    def test_help_does_not_start_setup(self):
        process = self.start(arguments=["--help"])
        self.finish(process)
        self.assertIn("--run-config", self.output())
        self.assertIn("--benchmarks", self.output())
        self.assertIn("science-0.1", self.output())
        self.assertEqual(self.events(), [])


if __name__ == "__main__":
    unittest.main()
