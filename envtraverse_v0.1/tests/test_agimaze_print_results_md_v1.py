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

from analysis_scripts.agimaze_single import print_results_md_v1 as report
from analysis_scripts.agimaze_single_old import print_results_md_v1 as legacy
from analysis_scripts.arc3 import print_results_md_cost as arc3_report


def entry(name: str = "TUTORIAL/STAGE-01/0000.json", steps: int = 5, max_steps: int = 10, solved: bool = True) -> dict:
    return {"name": name, "labyrinth_index": 0, "steps": steps, "max_steps": max_steps, "solved": solved}


def token_record(name: str = "TUTORIAL/STAGE-01/0000.json", input_tokens: int = 0,
                 cached: int = 0, output: int = 0) -> dict:
    return {
        "environment_id": name, "input_tokens": input_tokens,
        "cached_input_tokens": cached, "output_tokens": output,
        "run_folder": "/not/mounted/on/this/host/alias",
    }


class AgimazeReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "results.json"
        self.tokens_path = self.root / "raw_cost_tokens.json"
        self.stdout = self.enterContext(redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(redirect_stderr(io.StringIO()))
        # Rendering/validation tests use prepared analysis outputs. Real CLI
        # refresh behavior is exercised separately below without mocking subprocesses.
        self.analysis_run = self.enterContext(patch.object(report.subprocess, "run"))

    def write_results(self, payload) -> None:
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def write_tokens(self, payload) -> None:
        self.tokens_path.write_text(json.dumps(payload), encoding="utf-8")

    def test_markdown_matches_old_script_exactly(self) -> None:
        results = [report.LabyrinthResult(f"maze/{index}.json", steps, 10, solved)
                   for index, (steps, solved) in enumerate(((0, False), (10, True), (11, True), (20, True), (21, True), (50, True)))]
        self.assertEqual(report.render_markdown(results), legacy.render_markdown(results))

    def test_builtin_sort_matches_every_entry_in_old_order_file(self) -> None:
        paths = json.loads(legacy.ORDER_PATH.read_text())
        results = [report.LabyrinthResult(f"{name}/0000.json", 0, 70, False) for name in reversed(paths)]
        ordered = report.order_results(results)
        self.assertEqual([item.name for item in ordered], [f"{name}/0000.json" for name in paths])
        self.assertEqual(ordered, legacy.order_results(results))

    def test_new_categories_stages_and_multiple_filenames_sort_without_external_order(self) -> None:
        names = ["NEW/maze10.json", "TRAINING/S0-keys/STAGE-10/0000.json", "NEW/maze2.json",
                 "TRAINING/S0-keys/STAGE-02/0010.json", "TRAINING/S0-keys/STAGE-02/0002.json"]
        self.write_results([entry(name) for name in names])
        results = report.order_results(report.load_results(self.path))
        self.assertEqual([item.name for item in results], [names[4], names[3], names[1], names[2], names[0]])

    def test_score_and_tier_boundaries_match_old_rules(self) -> None:
        for steps, solved, tiers, score in (
            (0, False, (0, 0, 0), 0), (10, True, (1, 1, 1), 1),
            (20, True, (1, 1, 0), 0.5), (30, True, (1, 0, 0), 0.2),
            (50, True, (1, 0, 0), 1 / 17), (1, False, (0, 0, 0), 0),
        ):
            with self.subTest(steps=steps, solved=solved):
                result = report.LabyrinthResult("maze.json", steps, 10, solved)
                self.assertEqual(report.solved_tiers(result), tiers)
                self.assertAlmostEqual(report.score1(result), score)

    def test_after_analysis_main_reads_only_results_and_tokens(self) -> None:
        self.write_results([entry()])
        self.write_tokens([token_record(input_tokens=1_000_000, cached=200_000, output=100_000)])
        before = {path: path.read_bytes() for path in self.root.iterdir()}
        original_open = Path.open
        opened = []

        def checked_open(path, *args, **kwargs):
            self.analysis_run.assert_called_once_with(
                [sys.executable, str(report.ANALYSIS_PATH), str(self.root)], check=True, stdout=self.stderr,
            )
            self.assertIn(path, before)
            opened.append(path)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", checked_open):
            self.assertEqual(report.main([str(self.root)]), 0)
        self.assertEqual(opened, [self.path, self.tokens_path])
        self.assertEqual({path: path.read_bytes() for path in self.root.iterdir()}, before)
        self.assertIn("x5 solved: 1/1", self.stdout.getvalue())
        self.assertTrue(self.stdout.getvalue().endswith("total cost: $7.10\n\n1.000 0/0/0 5 7.10\n"))

    def test_empty_results_render_a_zero_summary(self) -> None:
        self.write_results([])
        self.write_tokens([])
        self.assertEqual(report.main([str(self.root)]), 0)
        output = self.stdout.getvalue()
        self.assertIn("x5 solved: 0/0", output)
        self.assertIn("total actions: 0", output)
        self.assertIn("average score1: 0.000", output)
        self.assertTrue(output.endswith("total cost: $0.00\n\n0.000 0/0/0 0 0.00\n"))

    def test_numeric_summary_uses_unsolved_counts_and_no_currency_or_grouping(self) -> None:
        results = [report.LabyrinthResult(f"maze{index}", steps, 10, solved)
                   for index, (steps, solved) in enumerate(((0, False), (10, True), (20, True), (30, True)))]
        self.assertEqual(report.render_numeric_summary(results, 1234.567), "0.425 1/2/3 60 1234.57")

    def test_cost_formula_and_prices_match_arc3(self) -> None:
        self.assertEqual((report.PRICE_INPUT_PER_1M, report.PRICE_CACHED_PER_1M, report.PRICE_OUTPUT_PER_1M),
                         (arc3_report.PRICE_INPUT_PER_1M, arc3_report.PRICE_CACHED_PER_1M, arc3_report.PRICE_OUTPUT_PER_1M))
        for counts, expected in (
            ((1_000_000, 200_000, 100_000), 7.10), ((1_000_000, 1_000_000, 0), 0.5),
            ((1_000_000, 0, 0), 5), ((0, 0, 1_000_000), 30), ((0, 0, 0), 0),
        ):
            with self.subTest(counts=counts):
                record = token_record(input_tokens=counts[0], cached=counts[1], output=counts[2])
                self.assertAlmostEqual(report.estimated_cost(record), expected)
                self.assertEqual(report.estimated_cost(record), arc3_report.estimated_cost(record))

    def test_total_cost_is_appended_without_changing_old_output(self) -> None:
        self.write_results([entry("maze2.json", solved=False), entry("maze1.json")])
        self.write_tokens([
            token_record("maze1.json", 1_000_000, 200_000, 100_000),
            token_record("maze2.json", 500_000, 400_000, 20_000),
        ])
        self.assertEqual(report.main([str(self.root)]), 0)
        old_output = legacy.render_markdown(report.order_results(report.load_results(self.path)))
        self.assertEqual(self.stdout.getvalue(), old_output + "\ntotal cost: $8.40\n\n0.500 1/1/1 10 8.40\n")

    def test_total_is_rounded_only_after_summing_games(self) -> None:
        self.write_results([entry("first"), entry("second")])
        self.write_tokens([token_record("first", output=100), token_record("second", output=100)])
        self.assertEqual(report.main([str(self.root)]), 0)
        self.assertTrue(self.stdout.getvalue().endswith("total cost: $0.01\n\n1.000 0/0/0 10 0.01\n"))

    def test_missing_or_malformed_token_file_is_an_error_without_partial_output(self) -> None:
        self.write_results([entry()])
        self.assertEqual(report.main([str(self.root)]), 1)
        self.assertIn("extract_raw_cost_tokens.py", self.stderr.getvalue())
        for content in ("[", "{}", "[null]", "[{}]", json.dumps([token_record(name=" ")])):
            with self.subTest(content=content):
                self.tokens_path.write_text(content, encoding="utf-8")
                self.assertEqual(report.main([str(self.root)]), 1)
        self.tokens_path.write_bytes(b"\xff")
        self.assertEqual(report.main([str(self.root)]), 1)
        self.assertEqual(self.stdout.getvalue(), "")

    def test_invalid_token_counts_are_errors_with_filename_and_maze_context(self) -> None:
        self.write_results([entry()])
        for field in ("input_tokens", "cached_input_tokens", "output_tokens"):
            for value in (-1, True, 1.5, "1", None):
                with self.subTest(field=field, value=value):
                    record = token_record()
                    record[field] = value
                    self.write_tokens([record])
                    self.assertEqual(report.main([str(self.root)]), 1)
            record = token_record()
            del record[field]
            self.write_tokens([record])
            self.assertEqual(report.main([str(self.root)]), 1)
        self.write_tokens([token_record(input_tokens=1, cached=2)])
        self.assertEqual(report.main([str(self.root)]), 1)
        self.assertIn("raw_cost_tokens.json: TUTORIAL/STAGE-01/0000.json", self.stderr.getvalue())
        self.assertIn("cannot exceed", self.stderr.getvalue())
        self.assertEqual(self.stdout.getvalue(), "")

    def test_duplicate_missing_or_extra_token_records_are_errors(self) -> None:
        self.write_results([entry()])
        for records, message in (
            ([], "missing token records"), ([token_record()] * 2, "duplicate environment_id"),
            ([token_record(), token_record("another.json")], "token records without results"),
        ):
            with self.subTest(records=records):
                self.write_tokens(records)
                self.assertEqual(report.main([str(self.root)]), 1)
                self.assertIn(message, self.stderr.getvalue())
        self.assertEqual(self.stdout.getvalue(), "")

    def test_markdown_escapes_special_characters(self) -> None:
        result = report.LabyrinthResult("a\\b|c\r\nd.json", 1, 1, True)
        self.assertIn("a\\\\b\\|c<br>d.json", report.render_markdown([result]))

    def test_invalid_results_fail_without_partial_output(self) -> None:
        invalid = [None, {}, [None], [{}], [entry(name=" ")], [entry(name=1)],
                   [entry(steps=-1)], [entry(steps=True)], [entry(steps="2")],
                   [entry(max_steps=0)], [entry(max_steps=False)], [entry(max_steps=1.5)],
                   [entry(solved=1)], [entry(solved="false")], [entry(), entry()]]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.write_results(payload)
                self.assertEqual(report.main([str(self.root)]), 1)
        self.assertEqual(self.stdout.getvalue(), "")

    def test_missing_or_malformed_file_is_an_error(self) -> None:
        self.assertEqual(report.main([str(self.root)]), 1)
        self.path.write_text("[", encoding="utf-8")
        self.assertEqual(report.main([str(self.root)]), 1)
        self.path.write_bytes(b"\xff")
        self.assertEqual(report.main([str(self.root)]), 1)
        self.assertEqual(self.stdout.getvalue(), "")

    def test_analysis_is_run_every_time_even_when_outputs_already_exist(self) -> None:
        self.write_results([entry()])
        self.write_tokens([token_record()])
        self.assertEqual(report.main([str(self.root)]), 0)
        self.assertEqual(report.main([str(self.root)]), 0)
        self.assertEqual(self.analysis_run.call_args_list, [call(
            [sys.executable, str(report.ANALYSIS_PATH), str(self.root)], check=True, stdout=self.stderr,
        )] * 2)

    def test_failed_analysis_never_reads_or_prints_stale_results(self) -> None:
        self.write_results([entry()])
        self.write_tokens([token_record()])
        for code, expected in ((17, 17), (-9, 1)):
            self.analysis_run.side_effect = subprocess.CalledProcessError(code, "analyse_runs.py")
            with self.subTest(code=code), patch.object(report, "load_results") as load_results:
                self.assertEqual(report.main([str(self.root)]), expected)
                load_results.assert_not_called()
        self.analysis_run.side_effect = OSError("Cannot start analysis")
        self.assertEqual(report.main([str(self.root)]), 1)
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertIn("analysis failed", self.stderr.getvalue())


class AgimazeReportCLITest(unittest.TestCase):
    def test_real_cli_refreshes_all_outputs_every_time_using_only_standard_library(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run with spaces"
            server_log = run / "alias/server/server.log"
            agent_log = run / "alias/run/agent.log"
            server_log.parent.mkdir(parents=True)
            agent_log.parent.mkdir(parents=True)
            prefix = "2026-09-09T10:00:00+0000 INFO [environment-owner] env-server.server: "
            maze = "TUTORIAL/STAGE-01/0000.json"
            solution = ["down", "left", "up", "left", "up", "right", "left", "down", "left"]
            for actions, usage, cost in (
                (solution, token_record(input_tokens=200_000_000, cached=100_000_000, output=50_000_000), "$2,050.00"),
                ([], token_record(input_tokens=1_000_000, cached=200_000, output=100_000), "$7.10"),
            ):
                with self.subTest(actions=actions):
                    server_log.write_text("\n".join([
                        f"{prefix}started session token=session requested_game_id=alias game_id={maze}",
                        *(f"{prefix}applied action session=session action={ {'id': action}!r} step_index={index}"
                          for index, action in enumerate(actions, start=1)),
                    ]) + "\n", encoding="utf-8")
                    agent_log.write_text("\n".join(json.dumps(event) for event in (
                        {"type": "thread.started", "thread_id": "thread"},
                        {"type": "turn.completed", "usage": usage},
                    )) + "\n", encoding="utf-8")
                    # Existing outputs, including corrupt ones, must never skip refresh.
                    for name in ("raw_actions.json", "raw_cost_tokens.json", "results.json"):
                        (run / name).write_text("stale output", encoding="utf-8")
                    result = subprocess.run(
                        [sys.executable, "-B", "-S", str(report.ANALYSIS_PATH.with_name("print_results_md_v1.py")), run.name],
                        cwd=root, text=True, capture_output=True, timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(result.stdout.startswith("Score1:"))
                    self.assertNotIn("Saved", result.stdout)
                    self.assertIn("raw_actions.json", result.stderr)
                    self.assertIn("raw_cost_tokens.json", result.stderr)
                    self.assertIn("Wrote", result.stderr)
                    self.assertIn(f"total actions: {len(actions)}", result.stdout)
                    self.assertIn(f"x5 solved: {int(bool(actions))}/1", result.stdout)
                    unsolved = int(not actions)
                    numeric_cost = cost.replace("$", "").replace(",", "")
                    summary = f"{float(bool(actions)):.3f} {unsolved}/{unsolved}/{unsolved} {len(actions)} {numeric_cost}"
                    self.assertTrue(result.stdout.endswith(f"total cost: {cost}\n\n{summary}\n"))
                    self.assertEqual(json.loads((run / "raw_actions.json").read_text())[0]["actions"],
                                     [{"id": action} for action in actions])
                    self.assertEqual(json.loads((run / "raw_cost_tokens.json").read_text())[0]["input_tokens"],
                                     usage["input_tokens"])
                    self.assertEqual(json.loads((run / "results.json").read_text())[0]["steps"], len(actions))


if __name__ == "__main__":
    unittest.main()
