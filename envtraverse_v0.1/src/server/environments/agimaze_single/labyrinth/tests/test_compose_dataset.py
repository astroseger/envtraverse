from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


COMPOSER_PATH = Path(__file__).parents[1] / "scripts" / "compose_dataset.py"
SPEC = importlib.util.spec_from_file_location("compose_dataset", COMPOSER_PATH)
assert SPEC is not None and SPEC.loader is not None
compose_dataset = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = compose_dataset
SPEC.loader.exec_module(compose_dataset)


class ComposeDatasetTest(unittest.TestCase):
    def examples_from_text(self, text: str) -> list[object]:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "trace.trace.txt"
            path.write_text(text, encoding="utf-8")
            return list(compose_dataset.examples_from_trace(path))

    def test_retains_nonterminal_transition(self) -> None:
        examples = self.examples_from_text(
            "<POS>(0, 0)</POS>\n"
            "<MAP>map-0</MAP>\n"
            "<ACT>right</ACT>\n"
            "<POS>(0, 1)</POS>\n"
            "<MAP>map-1</MAP>\n"
            "<ACT>left</ACT>\n"
            "<POS>(0, 0)</POS>\n"
        )
        self.assertEqual(
            examples,
            [
                compose_dataset.Example(
                    input="<MAP>map-0</MAP>\n<ACT>right</ACT>",
                    target="<POS>(0, 1)</POS>",
                ),
                compose_dataset.Example(
                    input="<MAP>map-1</MAP>\n<ACT>left</ACT>",
                    target="<POS>(0, 0)</POS>",
                ),
            ],
        )

    def test_excludes_only_transition_immediately_before_end(self) -> None:
        examples = self.examples_from_text(
            "<POS>(0, 0)</POS>\n"
            "<MAP>map-0</MAP>\n"
            "<ACT>right</ACT>\n"
            "<POS>(0, 1)</POS>\n"
            "<MAP>map-1</MAP>\n"
            "<ACT>up</ACT>\n"
            "<POS>(-1, 1)</POS>\n"
            "<MAP>map-exited</MAP>\n"
            "<END>Win</END>\n"
        )
        self.assertEqual(
            examples,
            [
                compose_dataset.Example(
                    input="<MAP>map-0</MAP>\n<ACT>right</ACT>",
                    target="<POS>(0, 1)</POS>",
                )
            ],
        )

    def test_rejects_end_before_a_completed_transition(self) -> None:
        with self.assertRaisesRegex(compose_dataset.TraceFormatError, "before any complete transition"):
            self.examples_from_text(
                "<POS>(0, 0)</POS>\n<MAP>map</MAP>\n<END>Win</END>\n"
            )

    def test_compose_deduplicates_identical_per_step_examples(self) -> None:
        traces, examples, records = self.compose_duplicate_traces(keep_duplicates=False)

        self.assertEqual((traces, examples), (2, 1))
        self.assertEqual(records, [self.expected_example_record()])

    def test_compose_keeps_duplicates_when_requested(self) -> None:
        traces, examples, records = self.compose_duplicate_traces(keep_duplicates=True)

        self.assertEqual((traces, examples), (2, 2))
        self.assertEqual(records, [self.expected_example_record(), self.expected_example_record()])

    def compose_duplicate_traces(self, *, keep_duplicates: bool) -> tuple[int, int, list[dict[str, str]]]:
        trace = (
            "<POS>(0, 0)</POS>\n"
            "<MAP>map-0</MAP>\n"
            "<ACT>right</ACT>\n"
            "<POS>(0, 1)</POS>\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "per_step"
            input_dir.mkdir()
            (input_dir / "first.trace.txt").write_text(trace, encoding="utf-8")
            (input_dir / "second.trace.txt").write_text(trace, encoding="utf-8")
            output = root / "per_step.jsonl"
            traces, examples = compose_dataset.compose(
                input_dir,
                output,
                overwrite=False,
                keep_duplicates=keep_duplicates,
            )
            records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
        return traces, examples, records

    @staticmethod
    def expected_example_record() -> dict[str, str]:
        return {
            "input": "<MAP>map-0</MAP>\n<ACT>right</ACT>",
            "target": "<POS>(0, 1)</POS>",
        }


if __name__ == "__main__":
    unittest.main()
