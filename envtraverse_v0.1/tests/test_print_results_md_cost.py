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

from analysis_scripts.arc3 import print_results_md_cost as report
from analysis_scripts.old_arc3 import print_results_md_cost as legacy


def token_record(game: str, input_tokens: int = 0, cached: int = 0, output: int = 0) -> dict:
    return {
        "environment_id": game,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "output_tokens": output,
        # This path can be stale or from another host. The report must not use it.
        "run_folder": "/not/mounted/on/this/host/alias",
    }


def scorecard(game: str, *, solved: int = 1, levels: int = 1, score: float = 100,
              level_actions: list[int] | None = None, state: str = "WIN") -> dict:
    counts = [5] if level_actions is None else level_actions
    return {
        "score": score,
        "total_levels_completed": solved,
        "total_levels": levels,
        "total_actions": sum(counts),
        "environments": [{
            "id": f"{game}-sdk-version",
            "runs": [{
                "levels_completed": solved,
                "actions": sum(counts),
                "level_actions": counts,
                "state": state,
            }],
        }],
    }


class CostTest(unittest.TestCase):
    def test_uses_requested_prices_and_does_not_double_charge_cached_input(self) -> None:
        self.assertEqual((report.PRICE_INPUT_PER_1M, report.PRICE_CACHED_PER_1M, report.PRICE_OUTPUT_PER_1M), (5, 0.5, 30))
        self.assertAlmostEqual(report.estimated_cost(token_record("ls20", 1_000_000, 200_000, 100_000)), 7.10)
        self.assertEqual(report.estimated_cost(token_record("ls20", 1_000_000, 1_000_000)), 0.5)
        self.assertEqual(report.estimated_cost(token_record("ls20", 1_000_000)), 5)
        self.assertEqual(report.estimated_cost(token_record("ls20", output=1_000_000)), 30)
        self.assertEqual(report.estimated_cost(token_record("ls20")), 0)

    def test_invalid_and_missing_token_counts_are_rejected(self) -> None:
        for field in ("input_tokens", "cached_input_tokens", "output_tokens"):
            for value in (-1, True, 1.5, "1", None):
                with self.subTest(field=field, value=value):
                    tokens = token_record("ls20")
                    tokens[field] = value
                    with self.assertRaisesRegex(ValueError, field):
                        report.estimated_cost(tokens)
            tokens = token_record("ls20")
            del tokens[field]
            with self.assertRaisesRegex(ValueError, field):
                report.estimated_cost(tokens)
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            report.estimated_cost(token_record("ls20", 10, 11))


class ResultsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.runs_dir = self.root / "first experiment" / "run"
        self.stdout = self.enterContext(redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(redirect_stderr(io.StringIO()))

    def write_run(self, path: Path, entries: list[tuple[dict, dict]]) -> None:
        path.mkdir(parents=True, exist_ok=True)
        (path / "raw_cost_tokens.json").write_text(json.dumps([tokens for tokens, _ in entries]), encoding="utf-8")
        for tokens, card in entries:
            (path / f"{tokens['environment_id']}_scorecard.json").write_text(json.dumps(card), encoding="utf-8")

    def sample_entries(self) -> list[tuple[dict, dict]]:
        return [
            (token_record("ls20", 1_000_000, 200_000, 100_000),
             scorecard("ls20", solved=2, levels=3, score=50, level_actions=[8, 12, 1600], state="NOT_FINISHED")),
            (token_record("m0r0"), scorecard("m0r0", solved=0, levels=6, score=0, level_actions=[0] * 6, state="NOT_FINISHED")),
            (token_record("bp35"), scorecard("bp35")),
        ]

    def test_exact_markdown_rows_and_summary_including_zero_usage(self) -> None:
        self.write_run(self.runs_dir, self.sample_entries())
        self.assertEqual(report.main([str(self.runs_dir)]), 0)
        self.assertEqual(self.stderr.getvalue(), "")
        self.assertEqual(self.stdout.getvalue(),
            "# Results\n\n"
            "| Game | Levels solved | Score | Steps on Solved | Steps on Unsolved | Estimated cost | Run Status |\n"
            "|---|---:|---:|---:|---:|---:|---|\n"
            "| bp35 | 1/1 | 100.00% | 5 | - | $0.00 | normal termination |\n"
            "| ls20 | 2/3 | 50.00% | 20 | 1600 | $7.10 | normal termination |\n"
            "| m0r0 | 0/6 | 0.00% | 0 | 0 | $0.00 | interrupted |\n"
            "\nfully solved games: 1/3\nmean per-game RHAE: 50.00%\ntotal cost: $7.10\n"
            "total number of actions: 1625\n")

    def test_output_matches_old_report_with_equivalent_cost_inputs(self) -> None:
        entries = self.sample_entries()
        self.write_run(self.runs_dir, entries)
        for tokens, _ in entries:
            game = tokens["environment_id"]
            cost = report.estimated_cost(tokens)
            (self.runs_dir / f"{game}_cost_estimation.json").write_text(json.dumps({
                "games": {game: {"estimated_cost_usd": cost}},
                "totals": {"estimated_cost_usd": cost},
            }), encoding="utf-8")
        old_output = io.StringIO()
        with redirect_stdout(old_output):
            legacy.print_results_md([self.runs_dir])
        report.print_results_md([self.runs_dir])
        self.assertEqual(self.stdout.getvalue(), old_output.getvalue())

    def test_reads_only_token_json_and_scorecards_and_does_not_modify_them(self) -> None:
        self.write_run(self.runs_dir, self.sample_entries())
        inputs = {path: path.read_bytes() for path in self.runs_dir.iterdir()}
        with patch.object(report, "read_json", wraps=report.read_json) as read:
            self.assertEqual(report.main([str(self.runs_dir)]), 0)
        self.assertEqual({call.args[0] for call in read.call_args_list}, set(inputs))
        self.assertEqual(read.call_count, len(inputs))
        self.assertEqual({path: path.read_bytes() for path in self.runs_dir.iterdir()}, inputs)

    def test_multiple_runs_have_mean_per_game_not_mean_per_row(self) -> None:
        other = self.root / "second experiment" / "run"
        self.write_run(self.runs_dir, [
            (token_record("ls20", 1_000_000), scorecard("ls20")),
            (token_record("bp35"), scorecard("bp35", score=60)),
        ])
        self.write_run(other, [
            (token_record("ls20", 1_000_000), scorecard("ls20", solved=0, score=0, level_actions=[10], state="GAME_OVER")),
            (token_record("cd82"), scorecard("cd82", score=90)),
        ])
        self.assertEqual(report.main([str(self.runs_dir), str(other)]), 0)
        output = self.stdout.getvalue()
        self.assertIn("| Game | Run index |", output)
        self.assertIn("| ls20 | first experiment |", output)
        self.assertIn("| ls20 | second experiment |", output)
        self.assertIn("fully solved games: 2/3", output)
        self.assertIn("mean per-game RHAE: 66.67%", output)
        self.assertIn("total cost: $10.00", output)
        self.assertIn("total number of actions: 25", output)

    def test_preserves_complete_environment_id_instead_of_truncating_it(self) -> None:
        self.write_run(self.runs_dir, [(token_record("ls20-specific-version"), scorecard("ls20-specific-version"))])
        self.assertEqual(report.main([str(self.runs_dir)]), 0)
        self.assertIn("| ls20-specific-version |", self.stdout.getvalue())

    def test_status_uses_old_win_or_1500_step_heuristic(self) -> None:
        for steps in (1499, 1500, 1501):
            with self.subTest(steps=steps):
                self.write_run(self.runs_dir, [(token_record("ls20"), scorecard(
                    "ls20", solved=1, levels=3, score=10, level_actions=[10, steps, 0], state="NOT_FINISHED",
                ))])
                result, = report.results_for_runs_dir(self.runs_dir, "first")
                self.assertEqual(result.table_row()[-1], "normal termination" if steps >= 1500 else "interrupted")
                self.assertEqual((result.steps_solved, result.steps_last), (10, steps))

    def test_missing_level_action_breakdown_preserves_old_fallback(self) -> None:
        for state in ("WIN", "NOT_FINISHED"):
            with self.subTest(state=state):
                card = scorecard("ls20", state=state)
                card["environments"][0]["runs"][0]["level_actions"] = None
                self.write_run(self.runs_dir, [(token_record("ls20"), card)])
                result, = report.results_for_runs_dir(self.runs_dir, "first")
                self.assertEqual(result.steps_solved, 5 if state == "WIN" else 0)
                self.assertEqual(result.steps_last, 5)

    def test_missing_inputs_fail_without_printing_partial_report(self) -> None:
        self.runs_dir.mkdir(parents=True)
        self.assertEqual(report.main([str(self.runs_dir)]), 1)
        self.assertIn("raw_cost_tokens.json", self.stderr.getvalue())
        self.assertEqual(self.stdout.getvalue(), "")
        self.write_run(self.runs_dir, [(token_record("ls20"), scorecard("ls20"))])
        (self.runs_dir / "ls20_scorecard.json").unlink()
        self.assertEqual(report.main([str(self.runs_dir)]), 1)
        self.assertIn("missing scorecards for ls20", self.stderr.getvalue())
        self.assertEqual(self.stdout.getvalue(), "")

    def test_scorecard_without_token_record_is_not_silently_omitted(self) -> None:
        self.write_run(self.runs_dir, [(token_record("ls20"), scorecard("ls20"))])
        (self.runs_dir / "raw_cost_tokens.json").write_text("[]", encoding="utf-8")
        self.assertEqual(report.main([str(self.runs_dir)]), 1)
        self.assertIn("missing token records for ls20", self.stderr.getvalue())
        self.assertEqual(self.stdout.getvalue(), "")

    def test_malformed_duplicate_and_unsafe_token_records_are_errors(self) -> None:
        self.runs_dir.mkdir(parents=True)
        path = self.runs_dir / "raw_cost_tokens.json"
        for content in ("not JSON", "{}", "[null]", "[{}]",
                        json.dumps([token_record("ls20")] * 2), json.dumps([token_record("../outside")])):
            with self.subTest(content=content):
                path.write_text(content, encoding="utf-8")
                self.assertEqual(report.main([str(self.runs_dir)]), 1)
                self.assertEqual(self.stdout.getvalue(), "")

    def test_invalid_token_counts_include_game_and_filename_in_error(self) -> None:
        self.write_run(self.runs_dir, [(token_record("ls20", 0, 1), scorecard("ls20"))])
        self.assertEqual(report.main([str(self.runs_dir)]), 1)
        self.assertIn("raw_cost_tokens.json: ls20: cached_input_tokens", self.stderr.getvalue())
        self.assertEqual(self.stdout.getvalue(), "")

    def test_malformed_or_ambiguous_scorecard_is_rejected(self) -> None:
        invalid_cards = [[], {}, {**scorecard("ls20"), "score": float("nan")},
                         {**scorecard("ls20"), "total_actions": -1},
                         {**scorecard("ls20"), "total_levels_completed": 2}]
        multiple_runs = scorecard("ls20")
        multiple_runs["environments"][0]["runs"] *= 2
        invalid_cards.append(multiple_runs)
        for card in invalid_cards:
            with self.subTest(card=card):
                self.write_run(self.runs_dir, [(token_record("ls20"), card)])
                self.assertEqual(report.main([str(self.runs_dir)]), 1)
                self.assertIn("ls20_scorecard.json", self.stderr.getvalue())
                self.assertEqual(self.stdout.getvalue(), "")

    def test_empty_run_reports_zero_totals(self) -> None:
        self.write_run(self.runs_dir, [])
        self.assertEqual(report.main([str(self.runs_dir)]), 0)
        self.assertIn("fully solved games: 0/0", self.stdout.getvalue())
        self.assertIn("mean per-game RHAE: 0.00%", self.stdout.getvalue())
        self.assertIn("total cost: $0.00", self.stdout.getvalue())
        self.assertIn("total number of actions: 0", self.stdout.getvalue())

    def test_duplicate_run_folder_argument_does_not_double_count(self) -> None:
        self.write_run(self.runs_dir, self.sample_entries())
        self.assertEqual(report.main([str(self.runs_dir), str(self.runs_dir / ".")]), 1)
        self.assertIn("each run folder only once", self.stderr.getvalue())
        self.assertEqual(self.stdout.getvalue(), "")

    def test_cli_runs_from_another_directory_without_any_logs_or_arc_sdk(self) -> None:
        self.write_run(self.runs_dir, self.sample_entries())
        result = subprocess.run(
            [sys.executable, "-S", str(ROOT / "analysis_scripts/arc3/print_results_md_cost.py"),
             str(self.runs_dir.relative_to(self.root))],
            cwd=self.root, text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("total cost: $7.10", result.stdout)
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
