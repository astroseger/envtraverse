from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


COMPOSER_PATH = Path(__file__).parents[1] / "scripts" / "compose_seq_dataset.py"
SPEC = importlib.util.spec_from_file_location("compose_seq_dataset", COMPOSER_PATH)
assert SPEC is not None and SPEC.loader is not None
compose_seq_dataset = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = compose_seq_dataset
SPEC.loader.exec_module(compose_seq_dataset)


class ComposeSeqDatasetTest(unittest.TestCase):
    def example_from_text(self, text: str, *, horizon: int) -> object | None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "trace.trace.txt"
            path.write_text(text, encoding="utf-8")
            return compose_seq_dataset.example_from_trace(path, horizon=horizon)

    @staticmethod
    def ordinary_trace() -> str:
        return (
            '<META>{"collection":"random"}</META>\n'
            "<MAP>map-with-agent-at-(0, 0)</MAP>\n"
            "<POS>(0, 0)</POS>\n"
            "<ACT>right</ACT>\n"
            "<POS>(0, 1)</POS>\n"
            "<ACT>down</ACT>\n"
            "<POS>(1, 1)</POS>\n"
            "<ACT>left</ACT>\n"
            "<POS>(1, 0)</POS>\n"
        )

    def test_uses_initial_map_and_first_h_actions_to_predict_pos_h(self) -> None:
        example = self.example_from_text(self.ordinary_trace(), horizon=2)

        self.assertEqual(
            example,
            compose_seq_dataset.Example(
                input="<MAP>map-with-agent-at-(0, 0)</MAP>\n<ACT>right</ACT>\n<ACT>down</ACT>",
                target="<POS>(1, 1)</POS>",
            ),
        )
        self.assertNotIn("<POS>", example.input)

    def test_horizon_one_predicts_position_after_first_action(self) -> None:
        example = self.example_from_text(self.ordinary_trace(), horizon=1)
        self.assertEqual(
            example,
            compose_seq_dataset.Example(
                input="<MAP>map-with-agent-at-(0, 0)</MAP>\n<ACT>right</ACT>",
                target="<POS>(0, 1)</POS>",
            ),
        )

    def test_skips_trace_shorter_than_horizon(self) -> None:
        self.assertIsNone(self.example_from_text(self.ordinary_trace(), horizon=4))

    def test_excludes_terminal_transition_before_selecting_horizon(self) -> None:
        trace = (
            "<MAP>map</MAP>\n"
            "<POS>(0, 0)</POS>\n"
            "<ACT>right</ACT>\n"
            "<POS>(0, 1)</POS>\n"
            "<ACT>up</ACT>\n"
            "<POS>(-1, 1)</POS>\n"
            "<END>Win</END>\n"
        )
        self.assertEqual(
            self.example_from_text(trace, horizon=1),
            compose_seq_dataset.Example(
                input="<MAP>map</MAP>\n<ACT>right</ACT>",
                target="<POS>(0, 1)</POS>",
            ),
        )
        self.assertIsNone(self.example_from_text(trace, horizon=2))

    def test_rejects_incomplete_or_misordered_trace(self) -> None:
        with self.assertRaisesRegex(compose_seq_dataset.TraceFormatError, "final ACT has no target POS"):
            self.example_from_text(
                "<MAP>map</MAP>\n<POS>(0, 0)</POS>\n<ACT>right</ACT>\n", horizon=1
            )
        with self.assertRaisesRegex(compose_seq_dataset.TraceFormatError, "POS appears before MAP"):
            self.example_from_text(
                "<POS>(0, 0)</POS>\n<MAP>map</MAP>\n", horizon=1
            )

    def test_compose_retains_duplicate_trajectories(self) -> None:
        trace = (
            "<MAP>map</MAP>\n"
            "<POS>(0, 0)</POS>\n"
            "<ACT>right</ACT>\n"
            "<POS>(0, 1)</POS>\n"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_dir = root / "seq"
            input_dir.mkdir()
            (input_dir / "first.trace.txt").write_text(trace, encoding="utf-8")
            (input_dir / "second.trace.txt").write_text(trace, encoding="utf-8")
            output = root / "seq.jsonl"

            traces, examples = compose_seq_dataset.compose(
                input_dir, output, horizon=1, overwrite=False
            )
            records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]

        expected = {"input": "<MAP>map</MAP>\n<ACT>right</ACT>", "target": "<POS>(0, 1)</POS>"}
        self.assertEqual((traces, examples), (2, 2))
        self.assertEqual(records, [expected, expected])

    def test_rejects_nonpositive_horizon(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 1"):
            self.example_from_text(self.ordinary_trace(), horizon=0)


if __name__ == "__main__":
    unittest.main()
