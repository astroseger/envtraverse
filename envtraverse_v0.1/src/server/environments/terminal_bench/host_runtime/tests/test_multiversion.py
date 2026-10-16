"""Version selection and pinning with local Git fixtures (no official task execution)."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from terminal_bench.catalog import BenchmarkCatalog, DATASETS, TB_REPOSITORY, split_game_id
from terminal_bench.datasets import fetch, git
from terminal_bench.protocol import RuntimeFault


class MultiVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tb_versions_")
        self.root = Path(self.temp.name)
        self.origin = self.root / "origin"
        self.origin.mkdir()
        git(self.origin, "init")
        git(self.origin, "config", "user.name", "Offline fixture")
        git(self.origin, "config", "user.email", "fixture@example.invalid")
        task = self.origin / "tasks" / "same-task"
        task.mkdir(parents=True)
        self.revisions = {}
        for version, timeout in [("3.0", 3600), ("4.0", 28800)]:
            (task / "instruction.md").write_text(f"SYNTHETIC ONLY: release {version}")
            (task / "task.toml").write_text(
                f'schema_version = "1.3"\n[agent]\ntimeout_sec = {timeout}\n'
                '[verifier]\nenvironment_mode = "separate"\n')
            git(self.origin, "add", ".")
            git(self.origin, "commit", "-m", version)
            git(self.origin, "tag", f"v{version}.0")
            self.revisions[version] = git(self.origin, "rev-parse", "HEAD")
        (task / "instruction.md").write_text("SYNTHETIC ONLY: unreleased moving HEAD")
        git(self.origin, "add", ".")
        git(self.origin, "commit", "-m", "unreleased")
        self.roots = {v: self.root / ("tb" + v) for v in self.revisions}

    def tearDown(self):
        self.temp.cleanup()

    def download(self, version, **kwargs):
        return fetch(self.roots[version], benchmark="terminal-bench-" + version,
                     repository=str(self.origin), **kwargs)

    def test_official_registry_defaults(self):
        for version in ("3.0", "4.0"):
            definition = DATASETS["terminal-bench-" + version]
            self.assertEqual(definition.repository, TB_REPOSITORY)
            self.assertEqual(definition.default_ref, "v" + version + ".0")

    def test_release_tags_not_moving_head_and_locks_are_separate(self):
        for version in self.roots:
            record = self.download(version)
            self.assertEqual(record["revision"], self.revisions[version])
            self.assertEqual(record["requested_ref"], "v" + version + ".0")
            self.assertFalse(record["explicit_ref_override"])
            self.assertNotEqual(record["revision"], git(self.origin, "rev-parse", "HEAD"))
            self.assertEqual(git(self.roots[version], "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
            self.assertEqual(self.download(version), record)

    def test_identically_named_tasks_route_to_independent_releases(self):
        for version in self.roots:
            self.download(version)
        catalog = BenchmarkCatalog(tb3_root=self.roots["3.0"], tb4_root=self.roots["4.0"])
        for version in self.roots:
            game = "terminal-bench-" + version + "-same-task"
            spec = catalog.resolve(game)
            self.assertEqual(spec.path, self.roots[version] / "tasks" / "same-task")
            self.assertIn(version, (spec.path / "instruction.md").read_text())
            self.assertEqual(spec.source["revision"], self.revisions[version])
        self.assertEqual(len(catalog.game_ids()), 3)

    def test_wrong_benchmark_lock_refused_by_fetch_and_runtime_catalog(self):
        self.download("3.0")
        with self.assertRaisesRegex(RuntimeError, "different benchmark"):
            fetch(self.roots["3.0"], benchmark="terminal-bench-4.0", repository=str(self.origin))
        with self.assertRaisesRegex(ValueError, "different benchmark"):
            BenchmarkCatalog(tb4_root=self.roots["3.0"])

    def test_same_unregistered_root_cannot_serve_two_versions(self):
        with self.assertRaisesRegex(ValueError, "own checkout"):
            BenchmarkCatalog(tb3_root=self.origin, tb4_root=self.origin)

    def test_explicit_override_recorded_and_reused_without_switching(self):
        record = self.download("3.0", ref=self.revisions["4.0"])
        self.assertTrue(record["explicit_ref_override"])
        self.assertEqual(record["revision"], self.revisions["4.0"])
        self.assertEqual(self.download("3.0"), record)
        with self.assertRaisesRegex(RuntimeError, "differs"):
            self.download("3.0", ref="v3.0.0")

    def test_missing_release_tag_fails_and_removes_temporary_clone(self):
        git(self.origin, "tag", "-d", "v3.0.0")
        with self.assertRaises(RuntimeError):
            self.download("3.0")
        self.assertFalse(self.roots["3.0"].exists())
        self.assertFalse(list(self.root.glob(".*download-*")))

    def test_orphan_lock_is_not_overwritten_by_fresh_download(self):
        lock = self.roots["3.0"].with_name(self.roots["3.0"].name + ".lock.json")
        lock.write_text('{"sentinel": true}')
        with self.assertRaisesRegex(RuntimeError, "Lock exists"):
            self.download("3.0")
        self.assertEqual(json.loads(lock.read_text()), {"sentinel": True})

    def test_unlocked_existing_checkout_must_match_default_release(self):
        git(None, "clone", "--", str(self.origin), str(self.roots["3.0"]))
        with self.assertRaisesRegex(RuntimeError, "differs"):
            self.download("3.0")

    def test_prefixes_and_names_with_hyphens(self):
        for version in ("2.1", "3.0", "4.0"):
            self.assertEqual(split_game_id(f"terminal-bench-{version}-task-with-hyphens"),
                             (f"terminal-bench-{version}", "task-with-hyphens"))
        for game in ["terminal-bench-3-x", "terminal-bench-4-x", "terminal-bench-3.0-",
                     "terminal-bench-4.0-../outside", "terminal-bench-5.0-future"]:
            with self.assertRaises(RuntimeFault):
                split_game_id(game)

    def test_missing_selected_version_is_error_not_silent_empty_list(self):
        proc = subprocess.run([sys.executable, "-m", "terminal_bench.datasets", "list",
                               "--benchmark", "terminal-bench-4.0"], capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not configured", proc.stderr)

    def test_cli_list_keeps_version_prefix_and_filter(self):
        for version in self.roots:
            self.download(version)
        proc = subprocess.run([sys.executable, "-m", "terminal_bench.datasets", "list",
                               "--tb3-root", str(self.roots["3.0"]),
                               "--tb4-root", str(self.roots["4.0"]),
                               "--benchmark", "terminal-bench-4.0", "--format", "yaml"],
                              check=True, capture_output=True, text=True)
        self.assertEqual(proc.stdout, "games:\n  - terminal-bench-4.0-same-task\n")

    def test_symlink_escape_refused_for_new_versions(self):
        for version in self.roots:
            self.download(version)
            (self.roots[version] / "tasks" / "escape").symlink_to(self.origin / "tasks" / "same-task")
        catalog = BenchmarkCatalog(tb3_root=self.roots["3.0"], tb4_root=self.roots["4.0"])
        for version in self.roots:
            with self.assertRaises(RuntimeFault):
                catalog.resolve("terminal-bench-" + version + "-escape")
