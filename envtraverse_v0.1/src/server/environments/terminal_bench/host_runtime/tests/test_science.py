"""Science release discovery and pinning using synthetic local Git fixtures."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from terminal_bench.catalog import BenchmarkCatalog, DATASETS, SMOKE_GAME, TBSCIENCE_REPOSITORY, split_game_id
from terminal_bench.datasets import fetch, git
from terminal_bench.protocol import RuntimeFault


BENCHMARK = "terminal-bench-science-0.1"
TASKS = ["life-sciences/biology/life-task", "physical-sciences/astronomy/shared-task"]
DRAFT = "engineering-sciences/mechanical-engineering/draft-task"


class ScienceDatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tb_science_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.origin = self.root / "origin"
        for relative in [*TASKS, DRAFT]:
            directory = self.origin / "tasks" / relative
            directory.mkdir(parents=True)
            (directory / "instruction.md").write_text("SYNTHETIC RELEASE FIXTURE ONLY")
            (directory / "task.toml").write_text(
                f'schema_version = "1.4"\n[task]\nname = "terminal-bench-science/{directory.name}"\n'
                '[agent]\ntimeout_sec = 28800\n[verifier]\nenvironment_mode = "separate"\n')
        self.manifest = self.origin / "tasks" / "dataset.toml"
        self.manifest.write_text(
            '[dataset]\nname = "terminal-bench-science/terminal-bench-science"\n' +
            "".join(f'[[tasks]]\nname = "terminal-bench-science/{Path(t).name}"\n' for t in TASKS))
        git(self.origin, "init")
        git(self.origin, "config", "user.name", "Offline fixture")
        git(self.origin, "config", "user.email", "fixture@example.invalid")
        git(self.origin, "add", ".")
        git(self.origin, "commit", "-m", "release")
        git(self.origin, "tag", "v0.1.0")
        self.revision = git(self.origin, "rev-parse", "HEAD")
        (self.origin / "tasks" / TASKS[0] / "instruction.md").write_text("SYNTHETIC MOVING HEAD")
        git(self.origin, "add", ".")
        git(self.origin, "commit", "-m", "unreleased")
        self.destination = self.root / "checkout"

    def fetch(self, **kwargs):
        return fetch(self.destination, benchmark=BENCHMARK, repository=str(self.origin), **kwargs)

    def test_release_pin_nested_ids_and_repeat_setup(self):
        definition = DATASETS[BENCHMARK]
        self.assertEqual(definition.repository, TBSCIENCE_REPOSITORY)
        self.assertEqual(definition.default_ref, "v0.1.0")
        record = self.fetch()
        self.assertEqual(record["revision"], self.revision)
        self.assertEqual(record["requested_ref"], "v0.1.0")
        self.assertEqual(record["task_count"], 2)
        self.assertFalse(record["explicit_ref_override"])
        self.assertEqual(git(self.destination, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
        self.assertEqual(self.fetch(), record)
        catalog = BenchmarkCatalog(tbscience_root=self.destination)
        ids = [BENCHMARK + "-" + relative for relative in TASKS]
        self.assertEqual(catalog.game_ids(), [SMOKE_GAME, *ids])
        self.assertEqual(self.destination.with_suffix(".game-ids.txt").read_text(), "\n".join(ids) + "\n")
        for game_id, relative in zip(ids, TASKS):
            spec = catalog.resolve(game_id)
            self.assertEqual(spec.path, self.destination / "tasks" / relative)
            self.assertEqual(spec.task_name, relative)
            self.assertEqual(spec.benchmark, BENCHMARK)
            self.assertEqual(spec.source["revision"], self.revision)
            self.assertEqual((spec.path / "instruction.md").read_text(), "SYNTHETIC RELEASE FIXTURE ONLY")

    def test_explicit_override_stays_pinned_on_repeat_setup(self):
        head = git(self.origin, "rev-parse", "HEAD")
        record = self.fetch(ref=head)
        self.assertEqual(record["revision"], head)
        self.assertTrue(record["explicit_ref_override"])
        self.assertEqual(self.fetch(), record)

    def test_science_lock_cannot_be_used_for_another_benchmark(self):
        self.fetch()
        with self.assertRaisesRegex(ValueError, "different benchmark"):
            BenchmarkCatalog(tb4_root=self.destination)

    def test_flat_and_nested_benchmarks_coexist(self):
        flat = self.root / "flat"
        shutil.copytree(self.origin / "tasks" / TASKS[1], flat / "tasks" / "shared-task")
        catalog = BenchmarkCatalog(flat, tbscience_root=self.origin)
        self.assertEqual(catalog.game_ids(), [SMOKE_GAME, "terminal-bench-2.1-shared-task",
                                             *(BENCHMARK + "-" + t for t in TASKS)])
        self.assertEqual(catalog.resolve("terminal-bench-2.1-shared-task").path,
                         flat / "tasks" / "shared-task")

    def test_cli_lists_nested_paths_as_yaml(self):
        result = subprocess.run([sys.executable, "-m", "terminal_bench.datasets", "list",
                                 "--tbscience-root", str(self.origin), "--benchmark", BENCHMARK,
                                 "--format", "yaml"], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, "games:\n" + "".join(f"  - {BENCHMARK}-{t}\n" for t in TASKS))

    def test_manifest_excludes_draft_and_leaf_only_ids(self):
        catalog = BenchmarkCatalog(tbscience_root=self.origin)
        for relative in [DRAFT, "shared-task"]:
            with self.subTest(relative=relative), self.assertRaises(RuntimeFault):
                catalog.resolve(BENCHMARK + "-" + relative)
        self.assertNotIn(BENCHMARK + "-" + DRAFT, catalog.game_ids())

    def test_relative_path_components_are_validated(self):
        self.assertEqual(split_game_id(BENCHMARK + "-" + TASKS[1]), (BENCHMARK, TASKS[1]))
        for relative in ["", "/absolute/task", "../task", "a/../task", "a/./task", "a//task",
                         "a/task/", "a\\task", "a/task name", "a/" + "x" * 129]:
            with self.subTest(relative=relative), self.assertRaises(RuntimeFault):
                split_game_id(BENCHMARK + "-" + relative)

    def test_nested_symlink_escape_is_rejected(self):
        outside = self.root / "outside"
        shutil.copytree(self.origin / "tasks" / TASKS[1], outside / "shared-task")
        (self.origin / "tasks" / "physical-sciences" / "escape").symlink_to(outside)
        catalog = BenchmarkCatalog(tbscience_root=self.origin)
        with self.assertRaisesRegex(RuntimeFault, "outside"):
            catalog.resolve(BENCHMARK + "-physical-sciences/escape/shared-task")
        self.assertEqual(len(catalog.game_ids()), 3)

    def test_manifest_is_required_and_must_identify_science(self):
        self.manifest.unlink()
        with self.assertRaises(FileNotFoundError):
            BenchmarkCatalog(tbscience_root=self.origin)
        for contents in ['[dataset]\nname = "wrong/dataset"\n',
                         '[dataset]\nname = "terminal-bench-science/terminal-bench-science"\n'
                         '[[tasks]]\nname = "terminal-bench-science/../task"\n']:
            self.manifest.write_text(contents)
            with self.assertRaises(ValueError):
                BenchmarkCatalog(tbscience_root=self.origin)

    def test_missing_or_misnamed_task_cannot_silently_shrink_the_suite(self):
        path = self.origin / "tasks" / TASKS[0] / "task.toml"
        path.write_text('[task]\nname = "terminal-bench-science/wrong-name"\n')
        catalog = BenchmarkCatalog(tbscience_root=self.origin)
        with self.assertRaisesRegex(RuntimeFault, "package name"):
            catalog.resolve(BENCHMARK + "-" + TASKS[0])
        with self.assertRaisesRegex(ValueError, "Missing manifest tasks.*life-task"):
            catalog.game_ids()
        path.unlink()
        with self.assertRaisesRegex(ValueError, "Missing manifest tasks.*life-task"):
            catalog.game_ids()

    def test_duplicate_manifest_entries_or_task_directories_are_rejected(self):
        original = self.manifest.read_text()
        self.manifest.write_text(original + '[[tasks]]\nname = "terminal-bench-science/life-task"\n')
        with self.assertRaisesRegex(ValueError, "Duplicate task"):
            BenchmarkCatalog(tbscience_root=self.origin)
        self.manifest.write_text(original)
        shutil.copytree(self.origin / "tasks" / TASKS[0],
                        self.origin / "tasks" / "another-domain" / "field" / "life-task")
        with self.assertRaisesRegex(ValueError, "Duplicate manifest task"):
            BenchmarkCatalog(tbscience_root=self.origin).game_ids()
