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

from analysis_scripts.agimaze_single import analyse_runs as analysis


TUTORIAL = "TUTORIAL/STAGE-01/0000.json"
SOLUTION = [{"id": action} for action in ("down", "left", "up", "left", "up", "right", "left", "down", "left")]


def record(name: str = TUTORIAL, actions: list | None = None) -> dict:
    return {"environment_id": name, "actions": [] if actions is None else actions, "run_folder": "/not/read/alias"}


class AgimazeAnalysisTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.raw_path = self.root / "raw_actions.json"
        self.results_path = self.root / "results.json"
        self.stdout = self.enterContext(redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(redirect_stderr(io.StringIO()))

    def write_raw(self, payload) -> None:
        self.raw_path.write_text(json.dumps(payload), encoding="utf-8")

    def test_raw_actions_keep_order_and_zero_action_games(self) -> None:
        records = [record("two.json"), record("one.json", [{"id": " up ", "extra": 1}])]
        self.write_raw(records)
        self.assertEqual(analysis.read_raw_actions(self.raw_path), records)

    def test_invalid_raw_records_are_rejected(self) -> None:
        invalid = [None, {}, [None], [{}], [record("")], [record(" ")], [record(12)],
                   [record(actions={})], [record(actions=[None])], [record(actions=[{}])],
                   [record(actions=[{"id": 1}])], [record(actions=[{"id": " "}])],
                   [{"name": TUTORIAL, "actions": []}], [record(), record()]]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.write_raw(payload)
                with self.assertRaisesRegex(ValueError, "raw_actions.json"):
                    analysis.read_raw_actions(self.raw_path)

    def test_replay_results_preserve_old_schema_and_raw_record_indices(self) -> None:
        replayer = Mock()
        replayer.replay.side_effect = [analysis.ReplayOutcome(70, True), analysis.ReplayOutcome(160, False)]
        records = [record("one.json", [{"id": "up"}]), record("two.json")]
        self.assertEqual(analysis.replay_results(records, replayer=replayer), [
            {"name": "one.json", "labyrinth_index": 0, "steps": 1, "max_steps": 70, "solved": True},
            {"name": "two.json", "labyrinth_index": 1, "steps": 0, "max_steps": 160, "solved": False},
        ])
        replayer.replay.assert_any_call("one.json", [{"id": "up"}])
        replayer.replay.assert_any_call("two.json", [])

    def test_empty_input_does_not_initialize_engine(self) -> None:
        with patch.object(analysis, "LabyrinthReplayer") as replayer:
            self.assertEqual(analysis.replay_results([]), [])
        replayer.assert_not_called()

    def test_after_extraction_only_raw_actions_is_read_from_the_run(self) -> None:
        self.write_raw([record(actions=SOLUTION)])
        before = self.raw_path.read_bytes()
        original_read = Path.read_text
        read_paths = []

        def checked_read(path, *args, **kwargs):
            self.assertEqual(path, self.raw_path)
            read_paths.append(path)
            return original_read(path, *args, **kwargs)

        replayer = Mock()
        replayer.replay.return_value = analysis.ReplayOutcome(70, True)
        with patch.object(analysis.subprocess, "run") as run, \
                patch.object(analysis, "LabyrinthReplayer", return_value=replayer), \
                patch.object(Path, "read_text", checked_read):
            self.assertEqual(analysis.main([str(self.root)]), 0)
        self.assertEqual(run.call_args_list, [
            call([sys.executable, str(path), str(self.root)], check=True)
            for path in analysis.EXTRACTOR_PATHS
        ])
        self.assertEqual(read_paths, [self.raw_path])
        self.assertEqual(self.raw_path.read_bytes(), before)
        self.assertEqual({p.name for p in self.root.iterdir()}, {"raw_actions.json", "results.json"})
        self.assertEqual(json.loads(self.results_path.read_text())[0]["steps"], 9)

    def test_extraction_failure_stops_without_reading_stale_raw_actions(self) -> None:
        self.write_raw([record()])
        self.results_path.write_text("previous results", encoding="utf-8")
        for stage, path in enumerate(analysis.EXTRACTOR_PATHS):
            for code, expected in ((17, 17), (-9, 1)):
                outcomes = [None] * stage + [subprocess.CalledProcessError(code, "extract")]
                with self.subTest(stage=stage, code=code), \
                        patch.object(analysis.subprocess, "run", side_effect=outcomes) as run, \
                        patch.object(analysis, "read_raw_actions") as read:
                    self.assertEqual(analysis.main([str(self.root)]), expected)
                    read.assert_not_called()
                    self.assertEqual(run.call_count, stage + 1)
                    self.assertIn(path.name, self.stderr.getvalue())
        self.assertEqual(self.results_path.read_text(), "previous results")

    def test_both_extractors_run_every_time_even_when_generated_files_exist(self) -> None:
        self.write_raw([])
        self.results_path.write_text("[]", encoding="utf-8")
        (self.root / "raw_cost_tokens.json").write_text("[]", encoding="utf-8")
        with patch.object(analysis.subprocess, "run") as run:
            self.assertEqual(analysis.main([str(self.root)]), 0)
            self.assertEqual(analysis.main([str(self.root)]), 0)
        self.assertEqual(run.call_args_list, [
            call([sys.executable, str(path), str(self.root)], check=True)
            for path in analysis.EXTRACTOR_PATHS
        ] * 2)

    def test_replay_error_preserves_old_results_and_reports_game(self) -> None:
        self.write_raw([record(actions=[{"id": "invalid"}])])
        self.results_path.write_text("previous results", encoding="utf-8")
        with patch.object(analysis.subprocess, "run"):
            self.assertEqual(analysis.main([str(self.root)]), 1)
        self.assertIn(TUTORIAL, self.stderr.getvalue())
        self.assertIn("Invalid action", self.stderr.getvalue())
        self.assertEqual(self.results_path.read_text(), "previous results")

    def test_invalid_json_and_launch_failure_are_reported(self) -> None:
        self.raw_path.write_text("[", encoding="utf-8")
        with patch.object(analysis.subprocess, "run"):
            self.assertEqual(analysis.main([str(self.root)]), 1)
        with patch.object(analysis.subprocess, "run", side_effect=OSError("cannot launch")):
            self.assertEqual(analysis.main([str(self.root)]), 1)
        self.assertFalse(self.results_path.exists())
        self.assertIn("cannot launch", self.stderr.getvalue())

    def test_atomic_write_replaces_results_and_cleans_temporary_file_on_failure(self) -> None:
        analysis.write_results(self.results_path, [{"name": "maze"}])
        previous = self.results_path.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("cannot replace")):
            with self.assertRaises(OSError):
                analysis.write_results(self.results_path, [])
        self.assertEqual(self.results_path.read_bytes(), previous)
        self.assertEqual(list(self.root.iterdir()), [self.results_path])
        analysis.write_results(self.results_path, [])
        self.assertEqual(json.loads(self.results_path.read_text()), [])

    def test_real_cli_extracts_replays_and_writes_results_from_another_directory(self) -> None:
        run = self.root / "run with spaces"
        server = run / "alias-one/server/server.log"
        server.parent.mkdir(parents=True)
        prefix = "2026-09-09T10:00:00+0000 INFO [environment-owner] env-server.server: "
        lines = [
            f"{prefix}started session token=one requested_game_id=alias-one game_id={TUTORIAL}",
            *(f"{prefix}applied action session=one action={action!r} step_index={i}"
              for i, action in enumerate(SOLUTION, start=1)),
        ]
        server.write_text("\n".join(lines) + "\n", encoding="utf-8")
        second_server = run / "alias-two/server/server.log"
        second_server.parent.mkdir(parents=True)
        second_server.write_text(f"{prefix}started session token=two requested_game_id=alias-two game_id=TUTORIAL/STAGE-02/0000.json\n")

        usage = {"input_tokens": 1000, "cached_input_tokens": 200, "output_tokens": 100}
        for alias in ("alias-one", "alias-two"):
            log = run / alias / "run/agent.log"
            log.parent.mkdir(parents=True)
            log.write_text("\n".join(json.dumps(event) for event in (
                {"type": "thread.started", "thread_id": alias},
                {"type": "turn.completed", "usage": usage},
            )) + "\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-B", "-S", str(analysis.ROOT / "analysis_scripts/agimaze_single/analyse_runs.py"), run.name],
            cwd=self.root, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        results = json.loads((run / "results.json").read_text())
        self.assertEqual(results[0], {"name": TUTORIAL, "labyrinth_index": 0, "steps": 9, "max_steps": 70, "solved": True})
        self.assertEqual(results[1]["steps"], 0)
        self.assertFalse(results[1]["solved"])
        raw = json.loads((run / "raw_actions.json").read_text())
        self.assertEqual(raw[0]["actions"], SOLUTION)
        self.assertEqual(raw[1]["actions"], [])
        tokens = json.loads((run / "raw_cost_tokens.json").read_text())
        self.assertEqual([item["environment_id"] for item in tokens], [TUTORIAL, "TUTORIAL/STAGE-02/0000.json"])
        for item in tokens:
            self.assertEqual({field: item[field] for field in usage}, usage)
        self.assertIn("1/2 solved, 9 actions", result.stdout)
        self.assertEqual({path.name for path in run.iterdir()}, {
            "alias-one", "alias-two", "raw_actions.json", "raw_cost_tokens.json", "results.json",
        })

    def test_empty_run_creates_empty_results(self) -> None:
        (self.root / "alias/server").mkdir(parents=True)
        (self.root / "alias/server/server.log").write_text("", encoding="utf-8")
        self.assertEqual(analysis.main([str(self.root)]), 0)
        self.assertEqual(json.loads(self.results_path.read_text()), [])
        self.assertEqual(json.loads((self.root / "raw_cost_tokens.json").read_text()), [])

    def test_missing_server_log_does_not_fall_back_to_old_raw_actions(self) -> None:
        self.write_raw([record()])
        self.assertEqual(analysis.main([str(self.root)]), 1)
        self.assertFalse(self.results_path.exists())


class LabyrinthReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.replayer = analysis.LabyrinthReplayer()

    def test_real_solution_unsolved_prefix_and_zero_actions(self) -> None:
        self.assertEqual(self.replayer.replay(TUTORIAL, SOLUTION), analysis.ReplayOutcome(70, True))
        for actions in ([], SOLUTION[:-1]):
            with self.subTest(actions=actions):
                self.assertEqual(self.replayer.replay(TUTORIAL, actions), analysis.ReplayOutcome(70, False))

    def test_actions_after_solution_and_unavailable_actions_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "solved after 9 actions"):
            self.replayer.replay(TUTORIAL, [*SOLUTION, {"id": "up"}])
        for action in ("RESET", "look_up", "bomb_up"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "Invalid action"):
                self.replayer.replay(TUTORIAL, [{"id": action}])

    def test_action_whitespace_and_extra_fields_match_server_normalization(self) -> None:
        actions = [{"id": f" {action['id']} ", "extra": True} for action in SOLUTION]
        self.assertEqual(self.replayer.replay(TUTORIAL, actions), analysis.ReplayOutcome(70, True))

    def test_extended_systems_are_enabled_as_in_server(self) -> None:
        for group, flag, action in (("boat", "boats_enabled", "up"),
                                    ("flashlight", "flashlight_enabled", "look_up"),
                                    ("grenades", "grenades_enabled", "bomb_up")):
            with self.subTest(group=group), \
                    patch.object(self.replayer, "_engine_class", wraps=self.replayer._engine_class) as engine:
                outcome = self.replayer.replay(f"EXTENDED/{group}/STAGE-01-4x4/0000.json", [{"id": action}])
                self.assertTrue(engine.call_args.kwargs[flag])
                self.assertFalse(outcome.solved)

    def test_missing_files_directories_and_paths_outside_packs_fail(self) -> None:
        for name in ("does-not-exist.json", "TUTORIAL/STAGE-01"):
            with self.subTest(name=name), self.assertRaises(FileNotFoundError):
                self.replayer.replay(name, [])
        for name in ("../outside.json", "/tmp/outside.json"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "escapes packs"):
                self.replayer.replay(name, [])

    def test_missing_or_invalid_baseline_budget_uses_default(self) -> None:
        payload = json.loads((analysis.LABYRINTH_PACKS_DIR / TUTORIAL).read_text())
        loaded = self.replayer._load_or_generate(payload)
        for value in (None, 0, -1, "70", True):
            loaded.config["metadata"] = {"max_steps": value}
            with self.subTest(value=value), patch.object(self.replayer, "_load_or_generate", return_value=loaded):
                self.assertEqual(self.replayer.replay(TUTORIAL, []).max_steps, 200)


if __name__ == "__main__":
    unittest.main()
