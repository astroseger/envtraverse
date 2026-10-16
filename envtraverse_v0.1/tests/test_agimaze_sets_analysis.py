from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis_scripts.agimaze_sets import analyse_ts_runs as analysis


TUTORIAL = "TUTORIAL/STAGE-01/0000.json"
SOLUTION = [{"id": action} for action in ("down", "left", "up", "left", "up", "right", "left", "down", "left")]


def record(set_id="curriculum", actions=None):
    return {
        "environment_id": set_id,
        "actions": [{"id": "start"}, {"id": "up"}, {"id": "start"}, *SOLUTION] if actions is None else actions,
        "run_folder": "/not/mounted/alias",
    }


class AgimazeSetsAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.sets = self.root / "sets"
        self.sets.mkdir()
        self.write_set("curriculum", [{"path": "earlier.json"}, {"path": TUTORIAL}])
        self.enterContext(patch.object(analysis, "SETS_DIR", self.sets))
        self.stdout = self.enterContext(redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(redirect_stderr(io.StringIO()))

    def write_set(self, name, payload):
        (self.sets / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")

    def extract(self, records):
        return analysis.extract_last_maze_actions(records, sets_dir=self.sets)

    def write_raw(self, records):
        (self.root / "raw_actions.json").write_text(json.dumps(records), encoding="utf-8")

    def test_target_actions_exclude_start_and_curriculum_and_preserve_source(self):
        raw = record()
        original = json.dumps(raw)
        targets = self.extract([raw])
        self.assertEqual(targets, [{
            "environment_id": TUTORIAL, "actions": SOLUTION,
            "run_folder": "/not/mounted/alias", "set_id": "curriculum", "labyrinth_index": 1,
        }])
        self.assertEqual(json.dumps(raw), original)
        results = analysis.replay_last_maze_results(targets)
        self.assertEqual(results, [{
            "name": TUTORIAL, "steps": 9, "max_steps": 70, "solved": True,
            "set_id": "curriculum", "labyrinth_index": 1,
        }])

    def test_starts_normalize_whitespace_like_server_and_allow_extra_fields(self):
        actions = [{"id": " start ", "extra": 1}, {"id": "up"}, {"id": "\tstart\n"}, {"id": " left ", "extra": True}]
        self.assertEqual(self.extract([record(actions=actions)])[0]["actions"], actions[-1:])

    def test_zero_action_target_is_included_as_unsolved(self):
        targets = self.extract([record(actions=[{"id": "start"}, {"id": "up"}, {"id": "start"}])])
        self.assertEqual(targets[0]["actions"], [])
        result = analysis.replay_last_maze_results(targets)[0]
        self.assertEqual(result["steps"], 0)
        self.assertFalse(result["solved"])

    def test_single_maze_set_and_repeated_mazes_use_final_position(self):
        self.write_set("one", [{"path": TUTORIAL}])
        self.write_set("repeated", [{"path": TUTORIAL}, {"path": TUTORIAL}])
        targets = self.extract([
            record("one", [{"id": "start"}, *SOLUTION]),
            record("repeated", [{"id": "start"}, {"id": "up"}, {"id": "start"}, {"id": "left"}]),
        ])
        self.assertEqual([t["labyrinth_index"] for t in targets], [0, 1])
        self.assertEqual([t["actions"] for t in targets], [SOLUTION, [{"id": "left"}]])
        replayer = Mock()
        replayer.replay.side_effect = [analysis.single.ReplayOutcome(70, True), analysis.single.ReplayOutcome(70, False)]
        results = analysis.replay_last_maze_results(targets, replayer=replayer)
        self.assertEqual([r["set_id"] for r in results], ["one", "repeated"])
        self.assertEqual([r["name"] for r in results], [TUTORIAL, TUTORIAL])
        self.assertEqual(replayer.replay.call_args_list, [call(TUTORIAL, SOLUTION), call(TUTORIAL, [{"id": "left"}])])

    def test_incorrect_start_counts_are_errors_not_silently_skipped_targets(self):
        for count in (0, 1, 3):
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, f"found {count} start actions; expected 2"):
                self.extract([record(actions=[{"id": "start"}] * count)])

    def test_actions_before_first_start_are_an_error(self):
        with self.assertRaisesRegex(ValueError, "before the first start"):
            self.extract([record(actions=[{"id": "up"}, {"id": "start"}, {"id": "start"}])])

    def test_invalid_missing_and_escaping_set_definitions_fail(self):
        for payload in (None, {}, [], [None], [{}], [{"path": " "}], [{"path": 1}],
                        [{"path": TUTORIAL, "tutorial": "false"}]):
            with self.subTest(payload=payload):
                self.write_set("invalid", payload)
                with self.assertRaises(ValueError):
                    analysis.load_set_paths("invalid", self.sets)
        (self.sets / "invalid.json").write_text("[", encoding="utf-8")
        with self.assertRaises(ValueError):
            analysis.load_set_paths("invalid", self.sets)
        with self.assertRaises(FileNotFoundError):
            analysis.load_set_paths("missing", self.sets)
        for name in ("../outside", "/tmp/outside", "a/b", "a\\b", "..", ".", ""):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Invalid set ID"):
                analysis.load_set_paths(name, self.sets)
        outside = self.root / "outside.json"
        outside.write_text('[{"path":"maze.json"}]', encoding="utf-8")
        (self.sets / "link.json").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "escapes sets directory"):
            analysis.load_set_paths("link", self.sets)

    def test_invalid_raw_records_and_duplicate_set_ids_are_rejected(self):
        for records in ([record(), record()], [record(actions=[{}])], [record(actions=[{"id": 1}])]):
            with self.subTest(records=records), patch.object(analysis.subprocess, "run"):
                self.write_raw(records)
                self.assertEqual(analysis.main([str(self.root)]), 1)
                self.assertFalse((self.root / analysis.RESULTS_FILENAME).exists())

    def test_main_refreshes_both_extractors_and_replaces_derived_outputs_every_time(self):
        self.write_raw([record()])
        with patch.object(analysis.subprocess, "run") as run:
            for _ in range(2):
                for name in (analysis.ACTIONS_FILENAME, analysis.RESULTS_FILENAME):
                    (self.root / name).write_text("stale", encoding="utf-8")
                self.assertEqual(analysis.main([str(self.root)]), 0)
        self.assertEqual(run.call_args_list, [
            call([sys.executable, str(path), str(self.root)], check=True)
            for path in analysis.EXTRACTOR_PATHS
        ] * 2)
        self.assertEqual(json.loads((self.root / analysis.ACTIONS_FILENAME).read_text())[0]["actions"], SOLUTION)
        self.assertTrue(json.loads((self.root / analysis.RESULTS_FILENAME).read_text())[0]["solved"])

    def test_each_extractor_failure_prevents_reading_stale_raw_data(self):
        for index, extractor in enumerate(analysis.EXTRACTOR_PATHS):
            for code in (17, -9):
                with self.subTest(extractor=extractor, code=code), \
                        patch.object(analysis.subprocess, "run", side_effect=[None] * index + [subprocess.CalledProcessError(code, "extract")]) as run, \
                        patch.object(analysis.single, "read_raw_actions") as read:
                    self.assertEqual(analysis.main([str(self.root)]), max(1, code))
                    read.assert_not_called()
                    self.assertEqual(run.call_count, index + 1)
                    self.assertIn(extractor.name, self.stderr.getvalue())

    def test_validation_and_replay_failures_preserve_both_previous_derived_files(self):
        for name in (analysis.ACTIONS_FILENAME, analysis.RESULTS_FILENAME):
            (self.root / name).write_text("previous", encoding="utf-8")
        for actions in ([], [{"id": "start"}, {"id": "up"}, {"id": "start"}, {"id": "invalid"}]):
            with self.subTest(actions=actions), patch.object(analysis.subprocess, "run"):
                self.write_raw([record(actions=actions)])
                self.assertEqual(analysis.main([str(self.root)]), 1)
                for name in (analysis.ACTIONS_FILENAME, analysis.RESULTS_FILENAME):
                    self.assertEqual((self.root / name).read_text(), "previous")

    def test_empty_run_does_not_initialize_replayer(self):
        self.write_raw([])
        with patch.object(analysis.subprocess, "run"), patch.object(analysis.single, "LabyrinthReplayer") as engine:
            self.assertEqual(analysis.main([str(self.root)]), 0)
            engine.assert_not_called()
        for name in (analysis.ACTIONS_FILENAME, analysis.RESULTS_FILENAME):
            self.assertEqual(json.loads((self.root / name).read_text()), [])


if __name__ == "__main__":
    unittest.main()
