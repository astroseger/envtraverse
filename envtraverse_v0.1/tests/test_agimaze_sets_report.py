from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import call, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis_scripts.agimaze_sets import print_ts_results_md_v1 as report


def result(set_id="curriculum", name="TRAINING/S0-keys/STAGE-01/0000.json", steps=9, solved=True):
    return {"set_id": set_id, "name": name, "labyrinth_index": 7, "steps": steps, "max_steps": 70, "solved": solved}


def tokens(set_id="curriculum"):
    return {"environment_id": set_id, "input_tokens": 1_000_000, "cached_input_tokens": 200_000, "output_tokens": 100_000}


class AgimazeSetsReportTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / report.RESULTS_FILENAME
        self.tokens_path = self.root / "raw_cost_tokens.json"
        self.stdout = self.enterContext(redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(redirect_stderr(io.StringIO()))
        self.analysis_run = self.enterContext(patch.object(report.subprocess, "run"))
        self.write_results([result()])
        self.write_tokens([tokens()])

    def write_results(self, payload):
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def write_tokens(self, payload):
        self.tokens_path.write_text(json.dumps(payload), encoding="utf-8")

    def test_same_metrics_as_single_report_but_cost_matches_set_ids(self):
        self.assertEqual(report.main([str(self.root)]), 0)
        results, set_ids = report.load_results(self.path)
        self.assertEqual(set_ids, {"curriculum"})
        self.assertIn(report.single.render_markdown(report.single.order_results(results)), self.stdout.getvalue())
        self.assertIn("x5 solved: 1/1", self.stdout.getvalue())
        self.assertIn("total actions: 9", self.stdout.getvalue())
        self.assertIn("average score1: 1.000", self.stdout.getvalue())
        self.assertTrue(self.stdout.getvalue().endswith(
            "total cost (whole sets, including curriculum): $7.10\n\n1.000 0/0/0 9 7.10\n"
        ))

    def test_numeric_summary_uses_target_unsolved_counts_and_whole_set_cost(self):
        self.write_results([
            result("unsolved", steps=0, solved=False), result("x1", steps=70),
            result("x2", steps=140), result("x5", steps=210),
        ])
        self.write_tokens([tokens(name) for name in ("unsolved", "x1", "x2", "x5")])
        self.assertEqual(report.main([str(self.root)]), 0)
        self.assertTrue(self.stdout.getvalue().endswith("$28.40\n\n0.425 1/2/3 420 28.40\n"))

    def test_analysis_runs_unconditionally_before_reading_only_results_and_tokens(self):
        original_read = Path.read_text
        opened = []

        def checked_read(path, *args, **kwargs):
            self.analysis_run.assert_called_once_with(
                [sys.executable, str(report.ANALYSIS_PATH), str(self.root)], check=True, stdout=self.stderr,
            )
            self.assertIn(path, (self.path, self.tokens_path))
            opened.append(path)
            return original_read(path, *args, **kwargs)

        with patch.object(Path, "read_text", checked_read):
            self.assertEqual(report.main([str(self.root)]), 0)
        self.assertEqual(opened, [self.path, self.tokens_path])
        self.assertEqual(report.main([str(self.root)]), 0)
        self.assertEqual(self.analysis_run.call_args_list, [call(
            [sys.executable, str(report.ANALYSIS_PATH), str(self.root)], check=True, stdout=self.stderr,
        )] * 2)

    def test_same_target_from_different_curricula_is_not_merged(self):
        self.write_results([result("first"), result("second", steps=0, solved=False)])
        self.write_tokens([tokens("first"), tokens("second")])
        self.assertEqual(report.main([str(self.root)]), 0)
        self.assertIn("0000.json (first)", self.stdout.getvalue())
        self.assertIn("0000.json (second)", self.stdout.getvalue())
        self.assertIn("x5 solved: 1/2", self.stdout.getvalue())
        self.assertTrue(self.stdout.getvalue().endswith("$14.20\n\n0.500 1/1/1 9 14.20\n"))

    def test_invalid_results_and_duplicate_set_ids_are_rejected(self):
        invalid = [None, {}, [None], [{}], [result(set_id="")], [result(set_id=1)],
                   [result(), result()], [result(name=" ")], [result(steps=-1)],
                   [result(solved="false")], [dict(result(), max_steps=0)]]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.write_results(payload)
                self.assertEqual(report.main([str(self.root)]), 1)
        self.assertEqual(self.stdout.getvalue(), "")

    def test_cost_requires_matching_sets_and_valid_counts(self):
        for payload in ([], [tokens("other")], [tokens(), tokens()], [tokens(), tokens("extra")],
                        [dict(tokens(), output_tokens=-1)], [dict(tokens(), cached_input_tokens=2_000_000)]):
            with self.subTest(payload=payload):
                self.write_tokens(payload)
                self.assertEqual(report.main([str(self.root)]), 1)
        self.assertEqual(self.stdout.getvalue(), "")

    def test_failed_analysis_never_loads_or_prints_stale_results(self):
        for code in (17, -9):
            with self.subTest(code=code), patch.object(report, "load_results") as read:
                self.analysis_run.side_effect = subprocess.CalledProcessError(code, "analyse_ts_runs.py")
                self.assertEqual(report.main([str(self.root)]), max(1, code))
                read.assert_not_called()
        self.analysis_run.side_effect = OSError("Cannot start analysis")
        self.assertEqual(report.main([str(self.root)]), 1)
        self.assertEqual(self.stdout.getvalue(), "")

    def test_empty_run_is_reported(self):
        self.write_results([])
        self.write_tokens([])
        self.assertEqual(report.main([str(self.root)]), 0)
        self.assertIn("x5 solved: 0/0", self.stdout.getvalue())
        self.assertIn("average score1: 0.000", self.stdout.getvalue())
        self.assertTrue(self.stdout.getvalue().endswith("$0.00\n\n0.000 0/0/0 0 0.00\n"))


class AgimazeSetsCLITest(unittest.TestCase):
    def test_real_cli_refreshes_extractors_and_target_outputs_from_another_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run with spaces"
            server_log = run / "alias/server/server.log"
            agent_log = run / "alias/run/agent.log"
            server_log.parent.mkdir(parents=True)
            agent_log.parent.mkdir(parents=True)
            set_id = "ts0_TRAINING_S0-keys_STAGE-01"
            prefix = "2026-09-16T10:00:00+0000 INFO [environment-owner] env-server.server: "
            derived_names = ("raw_actions.json", "raw_cost_tokens.json", "last_maze_actions.json", "last_maze_results.json")
            for target_actions, usage, cost in (([{"id": "up"}], tokens(set_id), "$7.10"), ([], dict(tokens(set_id), output_tokens=0), "$4.10")):
                with self.subTest(target_actions=target_actions):
                    actions = [{"id": "start"}, {"id": "up"}] * 7 + [{"id": "start"}] + target_actions
                    server_log.write_text("\n".join([
                        f"{prefix}started session token=session requested_game_id=alias game_id={set_id}",
                        *(f"{prefix}applied action session=session action={action!r} step_index={index}"
                          for index, action in enumerate(actions, start=1)),
                    ]) + "\n", encoding="utf-8")
                    agent_log.write_text("\n".join(json.dumps(event) for event in (
                        {"type": "thread.started", "thread_id": "thread"},
                        {"type": "turn.completed", "usage": usage},
                    )) + "\n", encoding="utf-8")
                    for name in derived_names:
                        (run / name).write_text("stale output", encoding="utf-8")
                    completed = subprocess.run(
                        [sys.executable, "-B", "-S", str(report.ANALYSIS_PATH.with_name("print_ts_results_md_v1.py")), run.name],
                        cwd=root, capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                    self.assertIn("Scores and action counts cover only the final maze", completed.stdout)
                    self.assertNotIn("Saved", completed.stdout)
                    for name in derived_names:
                        self.assertIn(name, completed.stderr)
                    self.assertIn(f"total actions: {len(target_actions)}", completed.stdout)
                    numeric_cost = cost.replace("$", "").replace(",", "")
                    summary = f"0.000 1/1/1 {len(target_actions)} {numeric_cost}"
                    self.assertTrue(completed.stdout.endswith(f"{cost}\n\n{summary}\n"))
                    raw = json.loads((run / "raw_actions.json").read_text())
                    target = json.loads((run / "last_maze_actions.json").read_text())[0]
                    result_row = json.loads((run / "last_maze_results.json").read_text())[0]
                    self.assertEqual(raw[0]["actions"], actions)
                    self.assertEqual(target["actions"], target_actions)
                    self.assertEqual(target["environment_id"], "TRAINING/S0-keys/STAGE-01/0000.json")
                    self.assertEqual(target["run_folder"], str(run / "alias"))
                    self.assertEqual(result_row["steps"], len(target_actions))
                    self.assertEqual(result_row["set_id"], set_id)
                    self.assertEqual(result_row["labyrinth_index"], 7)
                    self.assertFalse(result_row["solved"])
                    self.assertEqual(result_row["max_steps"], 70)
                    self.assertFalse((run / "results.json").exists())


if __name__ == "__main__":
    unittest.main()
