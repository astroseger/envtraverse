"""Offline tests using local Git repositories, never a substituted benchmark claim."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from terminal_bench.catalog import BenchmarkCatalog, SMOKE_GAME, split_game_id
from terminal_bench.datasets import fetch, git
from terminal_bench.protocol import RuntimeFault


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tb_git_")
        self.root = Path(self.temp.name)
        self.origin = self.root / "origin"
        self.origin.mkdir()
        git(self.origin, "init")
        git(self.origin, "config", "user.name", "Offline fixture")
        git(self.origin, "config", "user.email", "fixture@example.invalid")
        task = self.origin / "tasks" / "task-with-hyphens"
        task.mkdir(parents=True)
        (task / "task.toml").write_text('version = "1.0"\n')
        (task / "instruction.md").write_text("Synthetic test only")
        git(self.origin, "add", ".")
        git(self.origin, "commit", "-m", "fixture")
        self.commit = git(self.origin, "rev-parse", "HEAD")
        self.destination = self.root / "download"

    def tearDown(self):
        self.temp.cleanup()

    def test_clone_detaches_pins_lists_and_is_repeatable(self):
        record = fetch(self.destination, repository=str(self.origin))
        self.assertEqual(record["revision"], self.commit)
        self.assertEqual(record["task_count"], 1)
        self.assertEqual(git(self.destination, "rev-parse", "HEAD"), self.commit)
        self.assertEqual(fetch(self.destination, repository=str(self.origin)), record)
        self.assertEqual(git(self.destination, "status", "--porcelain"), "")
        catalog = BenchmarkCatalog(self.destination)
        self.assertIn("terminal-bench-2.1-task-with-hyphens", catalog.game_ids())
        self.assertIn(SMOKE_GAME, catalog.game_ids())
        self.assertEqual(catalog.resolve("terminal-bench-2.1-task-with-hyphens").source["revision"], self.commit)

    def test_explicit_commit_and_no_implicit_update(self):
        first = fetch(self.destination, self.commit, repository=str(self.origin))
        (self.origin / "new").write_text("later commit")
        git(self.origin, "add", ".")
        git(self.origin, "commit", "-m", "later")
        second = fetch(self.destination, repository=str(self.origin))
        self.assertEqual(first, second)
        self.assertEqual(git(self.destination, "rev-parse", "HEAD"), self.commit)

    def test_dirty_checkout_refused(self):
        fetch(self.destination, repository=str(self.origin))
        (self.destination / "modified").write_text("local edit")
        with self.assertRaisesRegex(RuntimeError, "local changes"):
            fetch(self.destination, repository=str(self.origin))

    def test_wrong_origin_or_existing_directory_refused(self):
        self.destination.mkdir()
        with self.assertRaises(RuntimeError):
            fetch(self.destination, repository=str(self.origin))
        self.destination.rmdir()
        fetch(self.destination, repository=str(self.origin))
        with self.assertRaisesRegex(RuntimeError, "different origin"):
            fetch(self.destination, repository="different-repository")

    def test_revision_mismatch_refused(self):
        fetch(self.destination, repository=str(self.origin))
        lock = self.destination.parent / (self.destination.name + ".lock.json")
        record = json.loads(lock.read_text())
        record["revision"] = "0" * 40
        lock.write_text(json.dumps(record))
        with self.assertRaisesRegex(RuntimeError, "lock record"):
            fetch(self.destination, repository=str(self.origin))

    def test_prefix_parse_and_invalid_names(self):
        self.assertEqual(split_game_id("terminal-bench-2.1-task-with-hyphens"),
                         ("terminal-bench-2.1", "task-with-hyphens"))
        self.assertEqual(split_game_id(SMOKE_GAME), ("smoke-test", "smoke-test"))
        for value in [None, {}, "smoke-test-other", "terminal-bench-2.1-", "terminal-bench-2.1-../x"]:
            with self.assertRaises(RuntimeFault):
                split_game_id(value)

    def test_missing_download_is_clear_error(self):
        with self.assertRaisesRegex(RuntimeFault, "not configured"):
            BenchmarkCatalog().resolve("terminal-bench-2.1-anything")
