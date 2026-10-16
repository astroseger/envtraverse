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

from analysis_scripts import summarize_agent_logs_in_dir as summary


def event(message: str, **fields) -> dict:
    return {"type": "external_agent_log", "message": message, **fields}


def inspection(iteration: int, step: int) -> dict:
    return event("iteration inspection", iteration_id=iteration, step_index=step)


def protocol(name: str) -> dict:
    return event("selected protocol", protocol=name)


def write_log(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")


class LogSummaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.log = self.root / "agent.log"

    def test_rows_use_global_steps_and_protocols_selected_after_each_inspection(self) -> None:
        write_log(self.log, [
            inspection(1, 10), protocol("normal_continuation_protocol"),
            inspection(2, 10), protocol("stuck_protocol"),
            inspection(3, 42), event("stop condition met"), event("stopping main loop"),
        ])
        self.assertEqual(summary.summarize_log(self.log), [
            "iteration=1 step=10/42 protocol=normal_continuation_protocol",
            "iteration=2 step=10/42 protocol=stuck_protocol",
            "iteration=3 step=42/42 protocol=-",
        ])

    def test_multiple_protocols_are_preserved_in_log_order(self) -> None:
        write_log(self.log, [protocol("before-inspection"), inspection(1, 3), protocol("one"), protocol("two")])
        self.assertEqual(summary.summarize_log(self.log), ["iteration=1 step=3/3 protocol=one,two"])

    def test_recovery_keeps_repeated_iteration_ids_without_cross_attaching_protocols(self) -> None:
        write_log(self.log, [
            inspection(1, 12), protocol("old"), event("iteration loop failed"),
            event("agent parameters"), event("starting agent", recovery=True),
            event("start init_recovery", step_index=20), protocol("recovery-before-inspection"),
            inspection(1, 25), protocol("new"), inspection(2, 30),
        ])
        self.assertEqual(summary.summarize_log(self.log), [
            "iteration=1 step=12/30 protocol=old",
            "iteration=1 step=25/30 protocol=new",
            "iteration=2 step=30/30 protocol=-",
        ])

    def test_lifecycle_boundaries_clear_pending_protocol_association(self) -> None:
        for message in ("agent parameters", "starting agent", "start init_recovery", "iteration loop failed",
                        "stopping main loop", "stop condition met", "initialization failed", "failed to inspect sessions"):
            with self.subTest(message=message):
                write_log(self.log, [inspection(1, 12), event(message), protocol("unrelated")])
                self.assertEqual(summary.summarize_log(self.log), ["iteration=1 step=12/12 protocol=-"])

    def test_later_recovery_checkpoint_can_supply_highest_known_step(self) -> None:
        write_log(self.log, [inspection(1, 10), event("start init_recovery", step_index=25), event("initialization failed")])
        self.assertEqual(summary.summarize_log(self.log), ["iteration=1 step=10/25 protocol=-"])

    def test_highest_logged_step_is_used_instead_of_summing_or_using_last_value(self) -> None:
        write_log(self.log, [inspection(1, 20), inspection(2, 10)])
        self.assertEqual(summary.summarize_log(self.log), [
            "iteration=1 step=20/20 protocol=-", "iteration=2 step=10/20 protocol=-",
        ])

    def test_codex_prose_nested_objects_and_tool_output_cannot_supply_progress(self) -> None:
        write_log(self.log, [
            {"type": "item.completed", "message": "iteration inspection", "iteration_id": 999, "step_index": 9999},
            {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "step: 9999"}},
            event("prompt body", prompt=json.dumps(inspection(999, 9999))),
            {"type": "item.completed", "item": protocol("fake")},
            {"type": "turn.completed", "usage": {"input_tokens": 100000}},
            inspection(1, 2),
            {"type": "item.completed", "message": "selected protocol", "protocol": "fake"},
        ])
        self.assertEqual(summary.summarize_log(self.log), ["iteration=1 step=2/2 protocol=-"])

    def test_legacy_global_counter_is_supported_without_environment_specific_fields(self) -> None:
        write_log(self.log, [
            event("iteration inspection", iteration_id=1, n_steps_total=30, current_level_index=2),
            event("iteration inspection", iteration_id=2, step_index=40, n_steps_total=9999),
        ])
        self.assertEqual(summary.summarize_log(self.log), [
            "iteration=1 step=30/40 protocol=-", "iteration=2 step=40/40 protocol=-",
        ])

    def test_no_inspections_does_not_invent_an_iteration(self) -> None:
        for events in ([], [event("starting agent"), event("initialization failed")]):
            with self.subTest(events=events):
                write_log(self.log, events)
                self.assertEqual(summary.summarize_log(self.log), [])

    def test_step_zero_and_missing_protocol_are_valid(self) -> None:
        write_log(self.log, [inspection(0, 0)])
        self.assertEqual(summary.summarize_log(self.log), ["iteration=0 step=0/0 protocol=-"])

    def test_invalid_fields_report_filename_and_line_number(self) -> None:
        bad_events = [event("iteration inspection", iteration_id=1), inspection(True, 1), inspection(-1, 1),
                      inspection("1", 1), inspection(1, -1), inspection(1, True), inspection(1, 1.5),
                      inspection(1, "1"), protocol(None), protocol("")]
        for bad in bad_events:
            with self.subTest(event=bad):
                write_log(self.log, [inspection(1, 1), bad])
                with self.assertRaisesRegex(ValueError, "agent.log:2:"):
                    summary.summarize_log(self.log)

    def test_malformed_or_truncated_json_is_reported_without_silently_losing_iterations(self) -> None:
        for text in ('{"type":', "not JSON", "[]", "null"):
            with self.subTest(text=text):
                self.log.write_text("\n" + text, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "agent.log:2:"):
                    summary.summarize_log(self.log)


class DirectorySummaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.runs_dir = self.root / "run with spaces"
        self.runs_dir.mkdir()
        self.stdout = self.enterContext(redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(redirect_stderr(io.StringIO()))

    def write_server_log(self, *mappings: tuple[str, str]) -> Path:
        prefix = "2026-09-09T10:00:00+0000 INFO [environment-owner] env-server.server: "
        for index, (alias, game) in enumerate(mappings):
            path = self.runs_dir / alias / "server/server.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as log:
                log.write(f"{prefix}started session token=token-{index} requested_game_id={alias} game_id={game}\n")
                log.write(f"{prefix}applied action session=token-{index} action=not-parsed step_index=9999\n")
        return path

    def test_exact_format_and_natural_game_order_for_any_environment(self) -> None:
        write_log(self.runs_dir / "alias-a/run/agent.log", [inspection(1, 3)])
        write_log(self.runs_dir / "alias-b/run/agent.log", [inspection(1, 2), protocol("continue"), inspection(2, 8)])
        self.write_server_log(("alias-a", "maze10-version"), ("alias-b", "maze2-version"))
        self.assertEqual(summary.main([str(self.runs_dir)]), 0)
        self.assertEqual(self.stdout.getvalue(),
            "alias-b (maze2-version)\n"
            "iteration=1 step=2/8 protocol=continue\niteration=2 step=8/8 protocol=-\n"
            "----------------------\nalias-a (maze10-version)\niteration=1 step=3/3 protocol=-\n")

    def test_opens_only_agent_and_server_logs_and_does_not_modify_inputs(self) -> None:
        agent_log = self.runs_dir / "alias/run/agent.log"
        write_log(agent_log, [inspection(1, 5)])
        server_log = self.write_server_log(("alias", "counter"))
        allowed = {agent_log, server_log}
        original_open = Path.open
        inputs = {path: path.read_bytes() for path in allowed}
        opened = []

        def checked_open(path, *args, **kwargs):
            self.assertIn(path, allowed)
            opened.append(path)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", checked_open):
            self.assertEqual(summary.main([str(self.runs_dir)]), 0)
        self.assertCountEqual(opened, allowed)
        self.assertEqual({path: path.read_bytes() for path in self.runs_dir.rglob("*") if path.is_file()}, inputs)

    def test_missing_server_log_or_mapping_shows_unknown_instead_of_guessing(self) -> None:
        write_log(self.runs_dir / "abcdef123/run/agent.log", [event("agent parameters", game_name="alias-only"), inspection(1, 1)])
        self.assertEqual(summary.main([str(self.runs_dir)]), 0)
        self.assertEqual(self.stdout.getvalue(), "abcdef123 (unknown)\niteration=1 step=1/1 protocol=-\n")

    def test_direct_log_fallback_prefers_nested_run_log_when_both_exist(self) -> None:
        write_log(self.runs_dir / "first/run/agent.log", [inspection(1, 1)])
        write_log(self.runs_dir / "first/agent.log", [inspection(9, 9999)])
        write_log(self.runs_dir / "second/agent.log", [inspection(2, 2)])
        self.assertEqual(summary.main([str(self.runs_dir)]), 0)
        output = self.stdout.getvalue()
        self.assertIn("first (unknown)\niteration=1 step=1/1", output)
        self.assertIn("second (unknown)\niteration=2 step=2/2", output)
        self.assertNotIn("9999", output)

    def test_empty_logs_are_reported_but_folders_without_logs_are_skipped(self) -> None:
        write_log(self.runs_dir / "empty/run/agent.log", [])
        (self.runs_dir / "not-started/run").mkdir(parents=True)
        self.assertEqual(summary.main([str(self.runs_dir)]), 0)
        self.assertEqual(self.stdout.getvalue(), "empty (unknown)\nNo iteration inspections recorded.\n")

    def test_one_invalid_log_does_not_prevent_other_games_from_being_summarized(self) -> None:
        bad_log = self.runs_dir / "bad/run/agent.log"
        bad_log.parent.mkdir(parents=True)
        bad_log.write_text("bad JSON\n", encoding="utf-8")
        write_log(self.runs_dir / "good/run/agent.log", [inspection(1, 10)])
        self.assertEqual(summary.main([str(self.runs_dir)]), 1)
        self.assertIn("ERROR:", self.stdout.getvalue())
        self.assertIn("good (unknown)\niteration=1 step=10/10 protocol=-", self.stdout.getvalue())

    def test_no_logs_and_missing_runs_dir_are_errors(self) -> None:
        self.assertEqual(summary.main([str(self.runs_dir)]), 1)
        self.assertIn("No agent.log files found", self.stderr.getvalue())
        self.assertEqual(summary.main([str(self.runs_dir / "missing")]), 1)
        self.assertIn("Runs folder does not exist", self.stderr.getvalue())

    def test_conflicting_game_names_are_errors(self) -> None:
        write_log(self.runs_dir / "alias/run/agent.log", [inspection(1, 1)])
        self.write_server_log(("alias", "game-one"), ("alias", "game-two"))
        self.assertEqual(summary.main([str(self.runs_dir)]), 1)
        self.assertIn("multiple sessions", self.stderr.getvalue())
        self.assertEqual(self.stdout.getvalue(), "")

    def test_malformed_session_start_does_not_guess_a_name(self) -> None:
        path = self.write_server_log(("alias", "counter"))
        path.write_text("2026-09-09T10:00:00+0000 INFO [environment-owner] env-server.server: started session broken\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "server.log:1: malformed"):
            summary.read_game_names(self.runs_dir)

    def test_cli_from_another_directory_uses_standard_library_and_no_observations(self) -> None:
        write_log(self.runs_dir / "alias/run/agent.log", [inspection(1, 7)])
        self.write_server_log(("alias", "counter"))
        result = subprocess.run(
            [sys.executable, "-S", str(ROOT / "analysis_scripts/summarize_agent_logs_in_dir.py"), self.runs_dir.name],
            cwd=self.root, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "alias (counter)\niteration=1 step=7/7 protocol=-\n")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
