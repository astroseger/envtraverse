from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis_scripts import recovery_warning


def write_log(path: Path, *messages: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(
        f"2026-09-09 12:00:00 +0000 agent_runner.py: {message}\n"
        for message in messages
    ), encoding="utf-8")


def write_agent_log(path: Path, *events: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(
        json.dumps({"type": "external_agent_log", **event}) + "\n"
        for event in events
    ), encoding="utf-8")


class RecoveryWarningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.runs_dir = self.root / "run with spaces"
        self.runs_dir.mkdir()
        self.log = self.runs_dir / "alias/run/agent_runner.log"
        self.stdout = self.enterContext(redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(redirect_stderr(io.StringIO()))

    def test_counts_attempts_without_double_counting_launch_and_exit(self) -> None:
        write_log(self.log,
                  "starting /usr/bin/python3 /home/user/run/agent.py --master alias",
                  "master stopped with exit code 1",
                  "inspection step_index=0 terminal=False",
                  "recovery attempt 1/10",
                  "starting /usr/bin/python3 /home/user/run/agent.py --recovery",
                  "recovery stopped with exit code 1",
                  "recovery attempt 2/10",
                  "starting /usr/bin/python3 /home/user/run/agent.py --recovery",
                  "recovery stopped with exit code 0")
        self.assertEqual(recovery_warning.count_recovery_attempts(self.log), 2)

    def test_does_not_confuse_other_recovery_mentions_with_attempts(self) -> None:
        write_log(self.log,
                  "master stopped before any step was made; not entering recovery",
                  "master stopped without a saved client session; not entering recovery",
                  "recovery attempt limit reached (10); stopping supervisor",
                  "agent.log is stale for 1800 seconds; stopping master",
                  "terminating stale agent pid=123",
                  "stop condition met; stopping supervisor")
        self.assertEqual(recovery_warning.count_recovery_attempts(self.log), 0)

    def test_counts_records_not_highest_attempt_number(self) -> None:
        write_log(self.log, "recovery attempt 3/10", "recovery attempt 1/10")
        self.assertEqual(recovery_warning.count_recovery_attempts(self.log), 2)

    def test_attempt_is_counted_even_without_a_following_launch_or_final_newline(self) -> None:
        self.log.parent.mkdir(parents=True)
        self.log.write_bytes(
            b"2026-09-09 12:00:00 +0400 agent_runner.py: recovery attempt 1/10\r\n"
            b"2026-09-09 12:00:01 -0400 agent_runner.py: recovery attempt 2/10"
        )
        self.assertEqual(recovery_warning.count_recovery_attempts(self.log), 2)

    def test_warns_per_game_in_folder_order_and_reports_total(self) -> None:
        write_log(self.runs_dir / "z/run/agent_runner.log", "recovery attempt 1/10")
        write_log(self.runs_dir / "a/run/agent_runner.log", "recovery attempt 1/10", "recovery attempt 2/10")
        write_log(self.runs_dir / "normal/run/agent_runner.log", "stop condition met; stopping supervisor")
        self.assertEqual(recovery_warning.main([str(self.runs_dir)]), 0)
        self.assertEqual(self.stdout.getvalue(),
                         "WARNING: a/run/agent_runner.log: 2 recovery attempt(s) (failure: 2)\n"
                         "WARNING: z/run/agent_runner.log: 1 recovery attempt(s) (failure: 1)\n"
                         "Found 3 recovery attempt(s) in 2 of 3 game(s).\n"
                         "Reasons: failure: 3\n")
        self.assertEqual(self.stderr.getvalue(), "")

    def test_groups_restarted_stop_reasons_and_failures_across_games(self) -> None:
        write_log(self.log, *(f"recovery attempt {i}/10" for i in range(1, 5)))
        write_agent_log(self.log.with_name("agent.log"),
                        {"message": "starting agent", "recovery": False},
                        {"message": "stop condition met", "reason": "no progress after stuck protocol"},
                        {"message": "starting agent", "recovery": True},
                        {"message": "iteration loop failed"},
                        {"message": "starting agent", "recovery": True},
                        {"message": "stop condition met", "reason": "custom stop"},
                        {"message": "starting agent", "recovery": True},
                        {"message": "stop condition met", "reason": "no progress after stuck protocol"},
                        {"message": "starting agent", "recovery": True},
                        {"message": "stop condition met", "reason": "environment is terminal"})
        other_log = self.runs_dir / "other/run/agent_runner.log"
        write_log(other_log, "recovery attempt 1/10")
        # The last stop reason still counts when the restarted agent later crashes.
        write_agent_log(other_log.with_name("agent.log"),
                        {"message": "starting agent", "recovery": False},
                        {"message": "stop condition met", "reason": "no progress after stuck protocol"},
                        {"message": "starting agent", "recovery": True},
                        {"message": "initialization failed"})
        self.assertEqual(recovery_warning.main([str(self.runs_dir)]), 0)
        self.assertEqual(self.stdout.getvalue(),
                         "WARNING: alias/run/agent_runner.log: 4 recovery attempt(s) "
                         "(custom stop: 1, failure: 1, no progress after stuck protocol: 2)\n"
                         "WARNING: other/run/agent_runner.log: 1 recovery attempt(s) "
                         "(no progress after stuck protocol: 1)\n"
                         "Found 5 recovery attempt(s) in 2 of 2 game(s).\n"
                         "Reasons: custom stop: 1, failure: 1, no progress after stuck protocol: 3\n")
        self.assertEqual(self.stderr.getvalue(), "")

    def test_excludes_final_nonterminal_stop_without_another_start(self) -> None:
        write_log(self.log, "recovery attempt 1/1", "recovery attempt limit reached (1); stopping supervisor")
        write_agent_log(self.log.with_name("agent.log"),
                        {"message": "starting agent", "recovery": False},
                        {"message": "iteration loop failed"},
                        {"message": "starting agent", "recovery": True},
                        {"message": "stop condition met", "reason": "no progress after stuck protocol"})
        self.assertEqual(recovery_warning.main([str(self.runs_dir)]), 0)
        self.assertIn("(failure: 1)", self.stdout.getvalue())
        self.assertNotIn("no progress", self.stdout.getvalue())

    def test_ignores_unrelated_records_and_treats_missing_reasons_as_failures(self) -> None:
        write_log(self.log, "recovery attempt 1/10", "recovery attempt 2/10")
        agent_log = self.log.with_name("agent.log")
        write_agent_log(agent_log,
                        {"message": "starting agent"},
                        {"type": "item.completed", "message": "stop condition met", "reason": "wrong type"},
                        {"message": "stop condition met later", "reason": "wrong message"},
                        {"message": "tool output", "item": {"message": "stop condition met", "reason": "nested"}},
                        {"message": "stop condition met"},
                        {"message": "starting agent"},
                        {"message": "stop condition met", "reason": None},
                        {"message": "stop condition met", "reason": ""},
                        {"message": "stop condition met", "reason": " "},
                        {"message": "stop condition met", "reason": ["invalid"]},
                        {"message": "starting agent"},
                        {"message": "stop condition met", "reason": "final stop"},
                        {"type": "item.completed", "message": "starting agent"},
                        {"message": "tool output", "item": {"message": "starting agent"}})
        with agent_log.open("a", encoding="utf-8") as log:
            log.write('\nnull\n[]\n"starting agent"\nnot JSON\n{"type": "external_agent_log"')
        self.assertEqual(recovery_warning.main([str(self.runs_dir)]), 0)
        self.assertEqual(self.stdout.getvalue(),
                         "WARNING: alias/run/agent_runner.log: 2 recovery attempt(s) (failure: 2)\n"
                         "Found 2 recovery attempt(s) in 1 of 1 game(s).\n"
                         "Reasons: failure: 2\n")
        self.assertEqual(self.stderr.getvalue(), "")

    def test_inconsistent_logs_report_error_instead_of_negative_failures(self) -> None:
        write_log(self.log, "recovery attempt 1/10")
        write_agent_log(self.log.with_name("agent.log"),
                        {"message": "starting agent"},
                        {"message": "stop condition met", "reason": "no progress"},
                        {"message": "starting agent"},
                        {"message": "stop condition met", "reason": "no progress"},
                        {"message": "starting agent"})
        self.assertEqual(recovery_warning.main([str(self.runs_dir)]), 1)
        self.assertIn("More restarted stop reasons than recovery attempts", self.stderr.getvalue())
        self.assertEqual(self.stdout.getvalue(), "")

    def test_clean_run_and_empty_logs_do_not_produce_warnings(self) -> None:
        write_log(self.log, "stop condition met; stopping supervisor")
        write_log(self.runs_dir / "empty/run/agent_runner.log")
        self.assertEqual(recovery_warning.main([str(self.runs_dir)]), 0)
        self.assertEqual(self.stdout.getvalue(), "No recovery attempts found in 2 agent_runner.log file(s).\n")

    def test_direct_log_fallback_prefers_nested_log_when_both_exist(self) -> None:
        write_log(self.log, "recovery attempt 1/10")
        write_log(self.runs_dir / "alias/agent_runner.log", "recovery attempt 1/10", "recovery attempt 2/10")
        write_log(self.runs_dir / "direct/agent_runner.log", "recovery attempt 1/10")
        self.assertEqual(recovery_warning.find_runner_logs(self.runs_dir), [
            self.log, self.runs_dir / "direct/agent_runner.log",
        ])
        self.assertEqual(recovery_warning.main([str(self.runs_dir)]), 0)
        self.assertIn("Found 2 recovery attempt(s) in 2 of 2 game(s).", self.stdout.getvalue())

    def test_does_not_scan_workspace_copies_or_count_missing_logs(self) -> None:
        write_log(self.log, "stop condition met; stopping supervisor")
        write_log(self.runs_dir / "alias/run/agent_run/copy/agent_runner.log", "recovery attempt 1/10")
        (self.runs_dir / "not-started/run").mkdir(parents=True)
        self.assertEqual(recovery_warning.main([str(self.runs_dir)]), 0)
        self.assertEqual(self.stdout.getvalue(), "No recovery attempts found in 1 agent_runner.log file(s).\n")

    def test_reads_only_runner_and_sibling_agent_logs_and_does_not_modify_inputs(self) -> None:
        write_log(self.log, "recovery attempt 1/10")
        agent_log = self.log.with_name("agent.log")
        write_agent_log(agent_log,
                        {"message": "starting agent"},
                        {"message": "stop condition met", "reason": "no progress"},
                        {"message": "starting agent"})
        for name in ("agent.log", "server/server.log", "alias/observations/state.txt"):
            write_log(self.runs_dir / name, "recovery attempt 1/10")
        inputs = {path: path.read_bytes() for path in self.runs_dir.rglob("*") if path.is_file()}
        original_open = Path.open
        opened = []

        def checked_open(path, *args, **kwargs):
            self.assertIn(path, (self.log, agent_log))
            opened.append(path)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", checked_open):
            self.assertEqual(recovery_warning.main([str(self.runs_dir)]), 0)
        self.assertEqual(opened, [self.log, agent_log])
        self.assertEqual({path: path.read_bytes() for path in self.runs_dir.rglob("*") if path.is_file()}, inputs)

    def test_no_logs_and_missing_or_non_directory_inputs_are_errors(self) -> None:
        file_path = self.root / "file"
        file_path.write_text("not a directory", encoding="utf-8")
        for path in (self.runs_dir, self.runs_dir / "missing", file_path):
            with self.subTest(path=path):
                self.assertEqual(recovery_warning.main([str(path)]), 1)
        self.assertIn("No agent_runner.log files found", self.stderr.getvalue())
        self.assertIn("Runs folder does not exist", self.stderr.getvalue())
        self.assertEqual(self.stdout.getvalue(), "")

    def test_invalid_encoding_reports_error_and_continues_other_games(self) -> None:
        self.log.parent.mkdir(parents=True)
        self.log.write_bytes(b"\xff")
        write_log(self.runs_dir / "other/run/agent_runner.log", "recovery attempt 1/10")
        self.assertEqual(recovery_warning.main([str(self.runs_dir)]), 1)
        self.assertIn(str(self.log), self.stderr.getvalue())
        self.assertIn("WARNING: other/run/agent_runner.log: 1 recovery attempt(s)", self.stdout.getvalue())
        self.assertNotIn("No recovery attempts", self.stdout.getvalue())

    def test_unreadable_log_does_not_claim_no_recoveries(self) -> None:
        write_log(self.log)
        with patch.object(Path, "open", side_effect=PermissionError("cannot read")):
            self.assertEqual(recovery_warning.main([str(self.runs_dir)]), 1)
        self.assertIn("cannot read", self.stderr.getvalue())
        self.assertEqual(self.stdout.getvalue(), "")

    def test_invalid_agent_log_encoding_reports_its_path_and_continues(self) -> None:
        write_log(self.log, "recovery attempt 1/10")
        agent_log = self.log.with_name("agent.log")
        agent_log.write_bytes(b"\xff")
        write_log(self.runs_dir / "other/run/agent_runner.log", "recovery attempt 1/10")
        self.assertEqual(recovery_warning.main([str(self.runs_dir)]), 1)
        self.assertIn(f"Error reading {agent_log}:", self.stderr.getvalue())
        self.assertIn("WARNING: other/run/agent_runner.log: 1 recovery attempt(s) (failure: 1)", self.stdout.getvalue())
        self.assertNotIn("Reasons:", self.stdout.getvalue())

    def test_cli_runs_from_another_directory_without_site_packages(self) -> None:
        write_log(self.log, "recovery attempt 1/10")
        result = subprocess.run(
            [sys.executable, "-B", "-S", str(ROOT / "analysis_scripts/recovery_warning.py"), self.runs_dir.name],
            cwd=self.root, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout,
                         "WARNING: alias/run/agent_runner.log: 1 recovery attempt(s) (failure: 1)\n"
                         "Found 1 recovery attempt(s) in 1 of 1 game(s).\n"
                         "Reasons: failure: 1\n")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
