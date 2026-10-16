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

from analysis_scripts.arc3 import print_results_md_cost_v2 as arc3_report
from analysis_scripts.terminal_bench import print_results_md_cost as report


SCRIPT = ROOT / "analysis_scripts/terminal_bench/print_results_md_cost.py"
SERVER_PREFIX = "2026-09-18T18:47:28+0000 INFO [MainThread] env-server.server: "
CYBER_ERROR = {"type": "turn.failed", "error": {"message": "SECURITY work: join the Cyber program"}}
INIT_FAILED = {"type": "external_agent_log", "level": "ERROR", "logger": "simple_agent",
               "message": "initialization failed"}


def write_events(path: Path, events: list[dict | str]) -> None:
    path.write_text("".join((event if isinstance(event, str) else json.dumps(event)) + "\n"
                            for event in events), encoding="utf-8")


class RewardTests(unittest.TestCase):
    def test_numeric_rewards_keep_fractional_scores(self):
        for value in (0, 1, 0.25, -0.5):
            with self.subTest(value=value):
                self.assertEqual(report.reward_score({"reward": value}), value)
                self.assertEqual(report.reward_score(value), value)

    def test_null_and_non_numeric_rewards_are_zero(self):
        for value in (None, True, False, "1", "NaN", [], {}, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.assertEqual(report.reward_score(value), 0)
                self.assertEqual(report.reward_score({"reward": value}), 0)
        self.assertEqual(report.reward_score({"another_metric": 1}), 0)


class LogTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.log = self.root / "agent.log"

    def test_final_pattern_and_multiple_encounters_count_once_per_game(self):
        write_events(self.log, [CYBER_ERROR, CYBER_ERROR, INIT_FAILED, "", "  "])
        self.assertEqual(report.read_agent_log(self.log), (True, True))

    def test_error_keywords_are_case_insensitive_and_only_checked_inside_error(self):
        valid = {"type": "turn.failed", "error": {"detail": {"message": "cyber SECURITY"}}}
        self.assertTrue(report.is_cyber_security_error(valid))
        for event in (
            {"type": "turn.failed", "error": {"message": "security work"}},
            {"type": "turn.failed", "error": {"message": "Cyber program"}},
            {"type": "turn.failed", "error": {"message": "timeout"}, "text": "cyber security"},
            {"type": "item.completed", "error": CYBER_ERROR["error"]},
            None, [], "cyber security",
        ):
            with self.subTest(event=event):
                self.assertFalse(report.is_cyber_security_error(event))

    def test_final_pattern_requires_the_exact_last_two_messages(self):
        cases = [
            [CYBER_ERROR, INIT_FAILED, {"type": "turn.completed"}],
            [CYBER_ERROR, {"type": "other"}, INIT_FAILED],
            [CYBER_ERROR, INIT_FAILED, "plain text after the failure"],
            [CYBER_ERROR, '{"truncated":', INIT_FAILED],
            [CYBER_ERROR, INIT_FAILED, "[]"],
            [CYBER_ERROR],
        ]
        for key, replacement in (("type", "other"), ("level", "INFO"),
                                 ("logger", "other"), ("message", "execution failed")):
            cases.append([CYBER_ERROR, INIT_FAILED | {key: replacement}])
        for events in cases:
            with self.subTest(events=events):
                write_events(self.log, events)
                self.assertEqual(report.read_agent_log(self.log), (False, True))

    def test_empty_logs_have_no_cyber_error_and_no_normal_stop(self):
        self.log.write_text("\n  \n")
        self.assertEqual(report.read_agent_log(self.log), (False, False))
        self.assertEqual(report.read_runner_log(self.log), (0, True))

    def test_runner_counts_attempt_records_not_launches_or_limit_messages(self):
        self.log.write_text(
            "2026-09-18 18:47:28 +0000 agent_runner.py: recovery attempt 1/10\n"
            "2026-09-18 18:47:29 +0000 agent_runner.py: starting python agent.py --recovery\n"
            "2026-09-18 18:47:30 +0000 agent_runner.py: recovery attempt 1/10\n"
            "2026-09-18 18:47:31 +0000 agent_runner.py: recovery attempt limit reached (10); stopping supervisor\n"
        )
        self.assertEqual(report.read_runner_log(self.log), (2, True))
        with self.log.open("a") as stream:
            stream.write("2026-09-18 18:47:32 +0000 " + report.NORMAL_STOP + "\n\n  \n")
        self.assertEqual(report.read_runner_log(self.log), (2, False))
        with self.log.open("a") as stream:
            stream.write("2026-09-18 18:47:33 +0000 agent_runner.py: interrupted\n")
        self.assertEqual(report.read_runner_log(self.log), (2, True))


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.project = self.root / "project with spaces"
        self.tokens_path = self.project / "run/raw_cost_tokens.json"

    def add_game(self, alias, game_id, reward, *, attempts=0, failed=False,
                 events=(), tokens=(0, 0, 0), termination_reason="finish"):
        result_path = self.project / "run_tb/results" / alias / "result.json"
        result_path.parent.mkdir(parents=True)
        result_path.write_text(json.dumps({
            "game_id": game_id, "session_id": alias, "rewards": reward,
            "termination_reason": termination_reason,
        }))
        logs = self.project / "run" / alias / "run"
        logs.mkdir(parents=True)
        messages = [f"agent_runner.py: recovery attempt {i}/10" for i in range(1, attempts + 1)]
        messages.append("agent_runner.py: recovery attempt limit reached (10); stopping supervisor"
                        if failed else report.NORMAL_STOP)
        (logs / "agent_runner.log").write_text("".join(
            "2026-09-18 18:47:28 +0000 " + message + "\n" for message in messages))
        usage = dict(zip(("input_tokens", "cached_input_tokens", "output_tokens"), tokens))
        write_events(logs / "agent.log", [
            {"type": "thread.started", "thread_id": alias},
            {"type": "turn.completed", "usage": usage}, *events,
        ])
        server = logs.parent / "server/server.log"
        server.parent.mkdir()
        server.write_text(SERVER_PREFIX + f"started session token=token-{alias} requested_game_id={alias} game_id={game_id}\n")
        return {"environment_id": game_id, "run_folder": "/old/host/path/" + alias, **usage}

    def sample(self):
        records = [
            self.add_game("failed", "task-b", None, attempts=10, failed=True,
                          termination_reason="agent_stopped",
                          events=[CYBER_ERROR, CYBER_ERROR, INIT_FAILED], tokens=(1_000_000, 200_000, 100_000)),
            self.add_game("recovered", "task-c", {"reward": 1}, attempts=6,
                          events=[CYBER_ERROR, INIT_FAILED]),
            self.add_game("normal", "task-a", {"reward": 1}),
        ]
        self.tokens_path.write_text(json.dumps(records))
        return records

    def test_report_includes_null_rewards_and_distinguishes_failed_recovery_from_encounters(self):
        self.sample()
        results = report.load_results(self.project)
        total_cost_tokens = report.load_total_cost_tokens(self.tokens_path, {r.game_id for r in results})
        self.assertEqual(report.render_markdown(results, total_cost_tokens),
            "| game_id | score | N_recovery |\n| ------- | ----: | ---------: |\n"
            "| task-a  |     1 |          0 |\n| task-b  |     0 |         10 |\n"
            "| task-c  |     1 |          6 |\n\n"
            "score: 0.666667 (2/3)\ncost tokens (M): 0.24\n"
            "n_fail_recovery: 1 (1 because of cybersecurity errors)\n"
            "n_encounter_cyber_security_error: 2\n"
            "agent_stopped: 1/1,  finish: 0/2")
        self.assertFalse(results[2].failed_because_of_cyber_security)
        self.assertTrue(results[2].encountered_cyber_security_error)

    def test_failed_recovery_without_final_cyber_pattern_is_not_attributed_to_cyber(self):
        self.add_game("a", "task-a", 0, failed=True,
                      events=[CYBER_ERROR, INIT_FAILED, {"type": "other"}])
        result, = report.load_results(self.project)
        self.assertTrue(result.failed_recovery)
        self.assertFalse(result.failed_because_of_cyber_security)
        self.assertTrue(result.encountered_cyber_security_error)

    def test_termination_failures_count_every_reward_other_than_numeric_one(self):
        cases = [
            ("finish", {"reward": value}) for value in (1, 1.0, 0, 0.5, 2, "1", True)
        ] + [
            ("agent_timeout", None), ("agent_timeout", {"reward": 1}),
            ("agent_stopped", 0), ("all_succeeded", 1),
        ]
        for index, (reason, reward) in enumerate(cases):
            self.add_game(str(index), f"task-{index}", reward, termination_reason=reason)
        results = report.load_results(self.project)
        self.assertEqual(report.render_markdown(results, 0).splitlines()[-1],
                         "agent_stopped: 1/1,  agent_timeout: 1/2,  all_succeeded: 0/1,  finish: 5/7")

    def test_missing_and_null_termination_reasons_are_counted_as_unknown(self):
        self.add_game("a", "task-a", 1, termination_reason=None)
        self.add_game("b", "task-b", None)
        path = self.project / "run_tb/results/b/result.json"
        raw = json.loads(path.read_text())
        del raw["termination_reason"]
        path.write_text(json.dumps(raw))
        results = report.load_results(self.project)
        self.assertEqual(report.render_markdown(results, 0).splitlines()[-1], "unknown: 1/2")

    def test_invalid_termination_reasons_fail(self):
        self.add_game("a", "task-a", 1)
        path = self.project / "run_tb/results/a/result.json"
        raw = json.loads(path.read_text())
        for reason in ("", "  ", 7, [], {}):
            with self.subTest(reason=reason):
                path.write_text(json.dumps(raw | {"termination_reason": reason}))
                with self.assertRaisesRegex(ValueError, "termination_reason"):
                    report.load_results(self.project)

    def test_cost_tokens_reuse_arc3_formula_and_cached_input_accounting(self):
        records = self.sample()
        self.assertIs(report.cost_tokens, arc3_report.cost_tokens)
        self.assertAlmostEqual(report.load_total_cost_tokens(self.tokens_path, {"task-a", "task-b", "task-c"}),
                               sum(arc3_report.cost_tokens(record) for record in records))

    def test_versioned_terminal_bench_game_ids_work_in_the_full_cli(self):
        self.add_game("alias", "terminal-bench-2.1-fix-git", {"reward": 1})
        result = subprocess.run([sys.executable, "-S", str(SCRIPT), str(self.project)],
                                cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("| terminal-bench-2.1-fix-git |     1 |          0 |", result.stdout)

    def test_cli_includes_setup_failure_without_a_started_session(self):
        game_id = "terminal-bench-2.1-fix-ocaml-gc"
        self.add_game("setup-failed", game_id, None, failed=True, termination_reason="error")
        folder = self.project / "run/setup-failed"
        (folder / "server/server.log").write_text(
            SERVER_PREFIX + f"starting requested_game_id=setup-failed game_id={game_id}\n"
            + SERVER_PREFIX + "request rejected: Task preparation failed\n",
        )
        write_events(folder / "run/agent.log", [INIT_FAILED])
        (folder / "run/agent_runner.log").write_text(
            "agent_runner.py: master stopped with exit code 1\n"
            "agent_runner.py: master stopped without a saved client session; not entering recovery\n",
        )
        self.add_game("success", "terminal-bench-2.1-fix-git", 1, tokens=(1_000_000, 0, 0))
        result = subprocess.run([sys.executable, "-S", str(SCRIPT), str(self.project)],
                                cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"| {game_id} |     0 |          0 |", result.stdout)
        self.assertIn("score: 0.500000 (1/2)", result.stdout)
        self.assertIn("cost tokens (M): 0.17", result.stdout)
        self.assertEqual(result.stdout.splitlines()[-1], "error: 1/1,  finish: 0/1")
        records = json.loads(self.tokens_path.read_text())
        self.assertEqual(len(records), 2)
        failed = next(record for record in records if record["environment_id"] == game_id)
        self.assertEqual([failed[key] for key in ("input_tokens", "cached_input_tokens", "output_tokens")], [0, 0, 0])

    def test_missing_extra_duplicate_or_invalid_cost_records_fail(self):
        records = self.sample()
        cases = [
            (records[:-1], "missing token records"),
            (records + [records[0] | {"environment_id": "extra"}], "without results"),
            (records + [records[0]], "duplicate environment_id"),
            ([records[0] | {"cached_input_tokens": 2_000_000}, *records[1:]], "cannot exceed"),
        ]
        for payload, message in cases:
            with self.subTest(message=message):
                self.tokens_path.write_text(json.dumps(payload))
                with self.assertRaisesRegex(ValueError, message):
                    report.load_total_cost_tokens(self.tokens_path, {"task-a", "task-b", "task-c"})

    def test_empty_or_missing_result_inputs_fail_instead_of_printing_zero_score(self):
        with self.assertRaisesRegex(ValueError, "No results found"):
            report.load_results(self.project)
        self.sample()
        (self.project / "run/normal/run/agent_runner.log").unlink()
        with self.assertRaises(FileNotFoundError):
            report.load_results(self.project)

    def test_duplicate_games_and_invalid_result_objects_fail(self):
        self.sample()
        path = self.project / "run_tb/results/normal/result.json"
        for payload, message in (([], "expected a result object"), ({}, "game_id"),
                                 ({"game_id": "task-b", "rewards": 1}, "duplicate game_id")):
            with self.subTest(payload=payload):
                path.write_text(json.dumps(payload))
                with self.assertRaisesRegex(ValueError, message):
                    report.load_results(self.project)

    def test_cli_refreshes_tokens_from_project_directory_and_keeps_stdout_markdown_only(self):
        self.sample()
        for stale in ("[]", "not JSON"):
            self.tokens_path.write_text(stale)
            result = subprocess.run([sys.executable, "-S", str(SCRIPT), self.project.name],
                                    cwd=self.root, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.startswith("| game_id | score | N_recovery |\n"))
            self.assertNotIn("Saved token", result.stdout)
            self.assertIn("cost tokens (M): 0.24", result.stdout)
            self.assertEqual(result.stdout.splitlines()[-1], "agent_stopped: 1/1,  finish: 0/2")
            self.assertIn("Saved token counts from 3 sessions", result.stderr)
            self.assertEqual(len(json.loads(self.tokens_path.read_text())), 3)

    def test_extractor_failure_does_not_print_a_stale_report(self):
        self.sample()
        with patch.object(report.subprocess, "run", side_effect=subprocess.CalledProcessError(17, ["extract"])), \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(report.main([str(self.project)]), 17)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("extract_raw_cost_tokens.py failed with exit code 17", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
