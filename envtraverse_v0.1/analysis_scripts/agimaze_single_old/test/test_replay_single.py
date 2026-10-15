from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from analysis_scripts import replay_single
from analysis_scripts import replay_single_terminal


class ReplaySingleTests(unittest.TestCase):
    def test_loads_labyrinth_index_from_results_and_actions_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            run_folder = Path(temporary_dir) / "run"
            run_folder.mkdir()
            actions_path = run_folder / "actions.json"
            results_path = run_folder / "results.json"

            actions_path.write_text(
                json.dumps(
                    [
                        {
                            "name": "two.json",
                            "actions": [{"id": "left"}, {"id": "down"}],
                        },
                        {"name": "one.json", "actions": [{"id": "up"}]},
                    ]
                ),
                encoding="utf-8",
            )
            results_path.write_text(
                json.dumps(
                    [
                        {"name": "one.json", "labyrinth_index": 3},
                        {"name": "two.json", "labyrinth_index": 7},
                    ]
                ),
                encoding="utf-8",
            )

            saved = replay_single.load_saved_labyrinth(run_folder, 7)

            self.assertEqual(saved.actions_path, actions_path)
            self.assertEqual(saved.results_path, results_path)
            self.assertEqual(saved.game_name, "run")
            self.assertEqual(saved.labyrinth_index, 7)
            self.assertEqual(saved.labyrinth_name, "two.json")
            self.assertEqual(saved.actions, ({"id": "left"}, {"id": "down"}))

    def test_missing_results_index_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            run_folder = Path(temporary_dir)
            (run_folder / "results.json").write_text(
                json.dumps([{"name": "one.json", "labyrinth_index": 0}]),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(IndexError, "was not found"):
                replay_single.load_saved_labyrinth(run_folder, 1)

    def test_terminal_player_uses_replay_single_module(self) -> None:
        self.assertIs(replay_single_terminal.replay_single, replay_single)


if __name__ == "__main__":
    unittest.main()
