"""Execute Bash entry points with explicit Python/Docker command-recording doubles.

These tests validate script dispatch and quoting, NOT Docker, Harbor or downloads.
Real local-Git downloads are covered separately in test_multiversion.py.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class ScriptDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tb_scripts_")
        self.root = Path(self.temp.name) / "package with spaces"
        self.root.mkdir()
        original = Path(__file__).resolve().parents[1]
        self.host = self.root / "src/server/environments/terminal_bench/host_runtime"
        self.host.mkdir(parents=True)
        for script in original.glob("*.sh"):
            shutil.copyfile(script, self.host / script.name)
        shutil.copyfile(original / "requirements.txt", self.host / "requirements.txt")
        self.log = self.root / "commands.jsonl"
        fake = self.host / ".venv" / "bin" / "python"
        fake.parent.mkdir(parents=True)
        fake.write_text(f"#!{sys.executable}\n" + r'''
import json, os, signal, sys, time
args = sys.argv[1:]
if args[:1] == ["-u"]:
    args = args[1:]
with open(os.environ['COMMAND_LOG'], 'a') as f:
    f.write(json.dumps(args) + '\n')
if args[:2] == ['-m', 'terminal_bench.datasets']:
    if args[2] == 'fetch' and os.environ.get('FAIL_BENCHMARK') in args:
        sys.exit(9)
    if args[2] == 'list':
        print('games:\n  - SCRIPT-DISPATCH-TEST-DOUBLE')
elif args[:2] == ['-m', 'terminal_bench.runtime']:
    if not os.environ.get('RUNTIME_EXIT'):
        # Real child process so the Bash test launcher's cleanup is exercised.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        while True:
            time.sleep(.02)
elif args[:1] == ['-']:
    sys.stdin.read()  # Do not execute the readiness program in dispatch-only tests.
else:
    print('COMMAND-RECORDING TEST DOUBLE; NO HARBOR OR DOCKER')
''')
        fake.chmod(0o755)
        docker = fake.parent / "docker"
        docker.write_text('#!/bin/sh\nexit 0\n')
        docker.chmod(0o755)
        self.env = dict(os.environ)
        for name in ["TB21_ROOT", "TB3_ROOT", "TB4_ROOT", "TB_VENV", "TB_RESULTS", "TB_TEST_OUTPUT",
                     "TB_ENVIRONMENT_KWARGS", "TB_ENVIRONMENT_TYPE", "TB_SOCKET_DIR", "TB_TEST_PARALLEL"]:
            self.env.pop(name, None)
        self.env.update(COMMAND_LOG=str(self.log), PYTHON=str(fake), BOOTSTRAP_PYTHON=str(fake),
                        PATH=str(fake.parent) + os.pathsep + os.environ["PATH"], RUNTIME_EXIT="1")
        self.fake = fake

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, script, *args, success=True, extra_env=None):
        env = self.env | (extra_env or {})
        proc = subprocess.run(["bash", str(self.host / script), *args],
                              cwd="/tmp", env=env, capture_output=True, text=True, timeout=15)
        if success:
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        else:
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def calls(self, module=None):
        calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return [c for c in calls if c[:2] == ['-m', module]] if module else calls

    def checkout(self, suffix):
        root = self.host / ".tb-data" / ("terminal-bench-" + suffix)
        (root / "tasks").mkdir(parents=True)
        return str(root)

    def test_setup_defaults_download_only_21(self):
        self.run_script("setup.sh")
        calls = self.calls("terminal_bench.datasets")
        fetches = [c for c in calls if c[2] == "fetch"]
        self.assertEqual([c[c.index("--benchmark")+1] for c in fetches], ["terminal-bench-2.1"])
        self.assertIn("SCRIPT-DISPATCH-TEST-DOUBLE", (self.host / "tb21-games.generated.yaml").read_text())
        self.assertFalse((self.host / "tb3-games.generated.yaml").exists())
        self.assertFalse((self.host / "tb4-games.generated.yaml").exists())

    def test_explicit_all_still_downloads_all_versions(self):
        self.run_script("setup.sh", "--benchmarks", "all")
        calls = [c for c in self.calls("terminal_bench.datasets") if c[2] == "fetch"]
        self.assertEqual([c[c.index("--benchmark")+1] for c in calls],
                         ["terminal-bench-2.1", "terminal-bench-3.0", "terminal-bench-4.0"])
        for label in ["tb21", "tb3", "tb4"]:
            self.assertIn("SCRIPT-DISPATCH-TEST-DOUBLE", (self.host / f"{label}-games.generated.yaml").read_text())

    def test_default_setup_does_not_modify_existing_optional_checkout_lists(self):
        self.checkout("3-0")
        self.checkout("4-0")
        for label in ["tb3", "tb4"]:
            (self.host / f"{label}-games.generated.yaml").write_text("EXISTING OPTIONAL LIST")
        self.run_script("setup.sh")
        calls = [c for c in self.calls("terminal_bench.datasets") if c[2] == "fetch"]
        self.assertEqual(len(calls), 1)
        for label in ["tb3", "tb4"]:
            self.assertEqual((self.host / f"{label}-games.generated.yaml").read_text(),
                             "EXISTING OPTIONAL LIST")

    def test_setup_subset_and_version_specific_refs(self):
        self.run_script("setup.sh", "--benchmarks", "3.0,4.0", "--tb3-ref", "abc123", "--tb4-ref", "def456")
        calls = [c for c in self.calls("terminal_bench.datasets") if c[2] == "fetch"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][-2:], ["--ref", "abc123"])
        self.assertEqual(calls[1][-2:], ["--ref", "def456"])

    def test_invalid_setup_arguments_fail_before_install(self):
        for args in [("--benchmarks", "5.0"), ("--benchmarks", "3.0,3.0"),
                     ("--benchmarks", "3.0,"), ("--benchmarks", "3.0,,4.0"),
                     ("--tb3-ref",), ("--benchmarks", "4.0", "--ref", "wrong")]:
            with self.subTest(args=args):
                self.run_script("setup.sh", *args, success=False)
        self.assertEqual(self.calls(), [])

    def test_setup_stops_on_failed_download(self):
        self.run_script("setup.sh", "--benchmarks", "all", success=False, extra_env={"FAIL_BENCHMARK": "terminal-bench-3.0"})
        calls = [c for c in self.calls("terminal_bench.datasets") if c[2] == "fetch"]
        self.assertEqual(len(calls), 2)
        self.assertFalse((self.host / "tb4-games.generated.yaml").exists())

    def test_list_uses_only_existing_roots_and_passes_filter(self):
        root3 = self.checkout("3-0")
        self.run_script("list_tasks.sh", "--benchmark", "terminal-bench-3.0")
        calls = self.calls("terminal_bench.datasets")
        self.assertIn(root3, calls[0])
        self.assertNotIn("--tb21-root", calls[0])
        self.assertNotIn("--tb4-root", calls[0])

    def test_runtime_uses_shared_socket_and_project_results_from_any_cwd(self):
        for suffix in ("2-1", "3-0", "4-0"):
            self.checkout(suffix)
        self.run_script("run.sh")
        call = self.calls("terminal_bench.runtime")[0]
        for flag in ("--tb21-root", "--tb3-root", "--tb4-root"):
            self.assertIn(flag, call)
        self.assertEqual(call[call.index("--socket") + 1], "socket/runtime.sock")
        self.assertEqual(call[call.index("--output") + 1], str(self.root / "run_tb/results"))
        self.assertEqual(call[call.index("--log-file") + 1], str(self.root / "run_tb/runtime.log"))
        self.assertEqual(call[call.index("--runs-dir") + 1], str(self.root / "run"))
        self.assertTrue((self.root / "run_tb/socket").is_dir())
        self.assertFalse((self.root / "run").exists())

    def test_existing_run_or_run_tb_is_rejected_without_launch(self):
        for name in ("run", "run_tb"):
            path = self.root / name
            path.mkdir()
            self.run_script("run.sh", success=False)
            path.rmdir()
        self.assertEqual(self.calls(), [])

    def test_broken_symlink_run_is_also_rejected(self):
        (self.root / "run").symlink_to(self.root / "missing")
        self.run_script("run.sh", success=False)
        self.assertEqual(self.calls(), [])

    def test_help_does_not_install_or_require_docker(self):
        for script in ("setup.sh", "run.sh"):
            self.run_script(script, "--help")
        self.assertEqual(self.calls(), [])
