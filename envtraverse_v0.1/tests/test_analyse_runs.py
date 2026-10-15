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

from analysis_scripts.arc3 import analyse_runs


class AnalyseRunsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.run_folder = self.root / "run with spaces"
        self.stdout = self.enterContext(redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(redirect_stderr(io.StringIO()))

    def test_runs_both_scripts_in_order_with_same_interpreter_and_absolute_paths(self) -> None:
        with patch.object(analyse_runs.subprocess, "run") as run:
            self.assertEqual(analyse_runs.main([str(self.run_folder)]), 0)
        self.assertEqual(run.call_args_list, [
            call([sys.executable, str(ROOT / "analysis_scripts/extract_raw_cost_tokens.py"), str(self.run_folder)], check=True),
            call([sys.executable, str(ROOT / "analysis_scripts/arc3/generate_scorecards_by_replay.py"), str(self.run_folder)], check=True),
        ])

    def test_token_extraction_failure_stops_before_replay(self) -> None:
        with patch.object(analyse_runs.subprocess, "run", side_effect=subprocess.CalledProcessError(17, "extract")) as run:
            self.assertEqual(analyse_runs.main([str(self.run_folder)]), 17)
        run.assert_called_once()
        self.assertIn("extract_raw_cost_tokens.py failed with exit code 17", self.stderr.getvalue())

    def test_replay_failure_is_returned(self) -> None:
        with patch.object(analyse_runs.subprocess, "run", side_effect=[None, subprocess.CalledProcessError(3, "replay")]) as run:
            self.assertEqual(analyse_runs.main([str(self.run_folder)]), 3)
        self.assertEqual(run.call_count, 2)
        self.assertIn("arc3/generate_scorecards_by_replay.py failed with exit code 3", self.stderr.getvalue())

    def test_child_signal_is_a_failure(self) -> None:
        with patch.object(analyse_runs.subprocess, "run", side_effect=subprocess.CalledProcessError(-9, "extract")):
            self.assertEqual(analyse_runs.main([str(self.run_folder)]), 1)

    def test_launch_error_is_reported_without_starting_next_step(self) -> None:
        with patch.object(analyse_runs.subprocess, "run", side_effect=OSError("cannot launch Python")) as run:
            self.assertEqual(analyse_runs.main([str(self.run_folder)]), 1)
        run.assert_called_once()
        self.assertIn("cannot launch Python", self.stderr.getvalue())

    def test_cli_runs_real_pipeline_from_another_directory_for_an_empty_run(self) -> None:
        server_dir = self.run_folder / "alias/server"
        server_dir.mkdir(parents=True)
        (server_dir / "server.log").write_text("", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(ROOT / "analysis_scripts/arc3/analyse_runs.py"), self.run_folder.name],
            cwd=self.root, text=True, capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads((self.run_folder / "raw_cost_tokens.json").read_text()), [])
        self.assertEqual(json.loads((self.run_folder / "raw_actions.json").read_text()), [])
        self.assertLess(result.stdout.index("Running extract_raw_cost_tokens.py"),
                        result.stdout.index("Running arc3/generate_scorecards_by_replay.py"))
        self.assertNotIn("# Results", result.stdout)
        self.assertEqual({path.name for path in self.run_folder.iterdir()}, {"alias", "raw_cost_tokens.json", "raw_actions.json"})

    def test_cli_missing_server_log_fails_before_replay(self) -> None:
        self.run_folder.mkdir()
        result = subprocess.run(
            [sys.executable, str(ROOT / "analysis_scripts/arc3/analyse_runs.py"), str(self.run_folder)],
            cwd=self.root, text=True, capture_output=True, timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("server.log", result.stderr)
        self.assertNotIn("Running arc3/generate_scorecards_by_replay.py", result.stdout)
        self.assertEqual(list(self.run_folder.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
