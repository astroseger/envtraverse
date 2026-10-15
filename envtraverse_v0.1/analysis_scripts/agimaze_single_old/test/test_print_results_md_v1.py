from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from analysis_scripts import print_results_md_v1


class PrintResultsMarkdownTests(unittest.TestCase):
    def test_main_accepts_any_json_filename_and_applies_default_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            run_folder = Path(temporary_dir)
            results_path = run_folder / "results.json"
            results_path.write_text(
                json.dumps(
                    [
                        self.result(
                            "TRAINING/S0-keys/STAGE-02/anything.json", 15, 70, True
                        ),
                        self.result(
                            "TRAINING/S0-keys/STAGE-01/0009.json", 10, 70, True
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = print_results_md_v1.main([str(run_folder)])

            output = stdout.getvalue()
            self.assertEqual(exit_code, 0)
            self.assertLess(output.index("STAGE-01"), output.index("STAGE-02"))
            self.assertIn("total actions: 25", output)

    def test_custom_order_file_controls_result_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            order_path = Path(temporary_dir) / "order.json"
            order_path.write_text(
                json.dumps(["suite/second", "suite/unused", "suite/first"]),
                encoding="utf-8",
            )
            results = [
                print_results_md_v1.LabyrinthResult(
                    "suite/first/a.json", 10, 10, True
                ),
                print_results_md_v1.LabyrinthResult(
                    "suite/second/b.json", 20, 10, True
                ),
            ]

            ordered = print_results_md_v1.order_results(results, order_path)

            self.assertEqual(
                [result.name for result in ordered],
                ["suite/second/b.json", "suite/first/a.json"],
            )

    def test_different_filenames_in_one_labyrinth_path_are_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            order_path = Path(temporary_dir) / "order.json"
            order_path.write_text(json.dumps(["suite/stage"]), encoding="utf-8")
            results = [
                print_results_md_v1.LabyrinthResult(
                    "suite/stage/0000.json", 10, 10, True
                ),
                print_results_md_v1.LabyrinthResult(
                    "suite/stage/0009.json", 20, 10, True
                ),
            ]

            with self.assertRaisesRegex(
                print_results_md_v1.ResultOrderingError,
                "Duplicate labyrinth paths",
            ):
                print_results_md_v1.order_results(results, order_path)

    def test_missing_order_name_is_a_critical_error_without_partial_table(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            run_folder = Path(temporary_dir)
            (run_folder / "results.json").write_text(
                json.dumps([self.result("unknown.json", 5, 10, False)]),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = print_results_md_v1.main([str(run_folder)])

            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("Critical error:", stderr.getvalue())
            self.assertIn("unknown.json", stderr.getvalue())

    @staticmethod
    def result(name: str, steps: int, max_steps: int, solved: bool) -> dict[str, object]:
        return {
            "name": name,
            "steps": steps,
            "max_steps": max_steps,
            "solved": solved,
        }


if __name__ == "__main__":
    unittest.main()
