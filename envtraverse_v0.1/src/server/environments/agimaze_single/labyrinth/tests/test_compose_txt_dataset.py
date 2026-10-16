from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


COMPOSER_PATH = Path(__file__).parents[1] / "scripts" / "compose_txt_dataset.py"
SPEC = importlib.util.spec_from_file_location("compose_txt_dataset", COMPOSER_PATH)
assert SPEC is not None and SPEC.loader is not None
compose_txt_dataset = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = compose_txt_dataset
SPEC.loader.exec_module(compose_txt_dataset)


class ComposeTxtDatasetTest(unittest.TestCase):
    @staticmethod
    def seq_trace(*, actions: tuple[str, ...] = ("right", "down")) -> str:
        transitions = "".join(
            f"<ACT>{action}</ACT>\n<POS>({index}, {index + 1})</POS>\n"
            for index, action in enumerate(actions)
        )
        return (
            '<META>{"session_id":"same-session"}</META>\n'
            "<MAP>\n"
            "####\n"
            "#S.#\n"
            "####\n"
            "</MAP>\n"
            "<POS>(0, 0)</POS>\n"
            f"{transitions}"
        )

    @staticmethod
    def txt_trace(*, actions: tuple[str, ...] = ("right", "down")) -> str:
        transitions = "".join(
            f"<ACT>{action}</ACT>\n<TXT>observation {index}</TXT>\n"
            for index, action in enumerate(actions)
        )
        return (
            '<META>{"session_id":"same-session"}</META>\n'
            "<START>\n"
            "board: 2x2\n"
            "start: (0, 0)\n"
            "</START>\n"
            f"{transitions}"
            "<END>Fail</END>\n"
        )

    def test_removes_txt_meta_and_prepends_seq_map_before_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            txt_path = root / "txt.trace.txt"
            seq_path = root / "seq.trace.txt"
            txt_path.write_text(self.txt_trace(), encoding="utf-8")
            seq_path.write_text(self.seq_trace(), encoding="utf-8")

            trace = compose_txt_dataset.compose_trace(txt_path, seq_path, check_actions=True)

        self.assertEqual(
            trace,
            "<MAP>\n"
            "####\n"
            "#S.#\n"
            "####\n"
            "</MAP>\n"
            "<START>\n"
            "board: 2x2\n"
            "start: (0, 0)\n"
            "</START>\n"
            "<ACT>right</ACT>\n"
            "<TXT>observation 0</TXT>\n"
            "<ACT>down</ACT>\n"
            "<TXT>observation 1</TXT>\n"
            "<END>Fail</END>\n",
        )

    def test_compose_matches_relative_filenames_and_writes_one_trace_record_each(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in ("first.trace.txt", "nested/second.trace.txt"):
                txt_path = root / "txt" / name
                seq_path = root / "seq" / name
                txt_path.parent.mkdir(parents=True, exist_ok=True)
                seq_path.parent.mkdir(parents=True, exist_ok=True)
                txt_path.write_text(self.txt_trace(), encoding="utf-8")
                seq_path.write_text(self.seq_trace(), encoding="utf-8")

            output = root / "composed.jsonl"
            traces, records = compose_txt_dataset.compose(
                root, output, overwrite=False, check_actions=True
            )
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]

        self.assertEqual((traces, records), (2, 2))
        self.assertEqual(len(rows), 2)
        self.assertEqual(set(rows[0]), {"trace"})
        self.assertNotIn("<META>", rows[0]["trace"])
        self.assertIn("</MAP>\n<START>", rows[0]["trace"])

    def test_optional_action_check_rejects_mismatched_traces(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            txt_path = root / "txt.trace.txt"
            seq_path = root / "seq.trace.txt"
            txt_path.write_text(self.txt_trace(actions=("right", "down")), encoding="utf-8")
            seq_path.write_text(self.seq_trace(actions=("right", "left")), encoding="utf-8")

            without_check = compose_txt_dataset.compose_trace(txt_path, seq_path)
            self.assertIn("<ACT>down</ACT>", without_check)
            with self.assertRaisesRegex(
                compose_txt_dataset.TraceFormatError, "ACT sequence differs"
            ):
                compose_txt_dataset.compose_trace(txt_path, seq_path, check_actions=True)

    def test_missing_matching_seq_trace_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            txt_path = root / "txt" / "orphan.trace.txt"
            txt_path.parent.mkdir(parents=True)
            txt_path.write_text(self.txt_trace(), encoding="utf-8")
            (root / "seq").mkdir()

            with self.assertRaisesRegex(FileNotFoundError, "Matching seq trace"):
                compose_txt_dataset.compose(root, root / "out.jsonl", overwrite=False)


if __name__ == "__main__":
    unittest.main()
