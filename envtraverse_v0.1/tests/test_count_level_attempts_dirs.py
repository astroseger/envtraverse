from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis_scripts.arc3 import count_level_attempts_dirs as counter


def frame(completed: int = 0, state: str = "NOT_FINISHED", action: str = "ACTION1", levels: int = 2) -> dict:
    return {
        "state": state, "levels_completed": completed, "win_levels": levels,
        "action_input": {"id": action, "data": {}},
    }


def write_steps(observations: Path, frames: list[dict], padded: bool = True) -> None:
    observations.mkdir(parents=True, exist_ok=True)
    for index, metadata in enumerate(frames):
        number = f"{index:04d}" if padded else str(index)
        (observations / f"step_{number}_metadata.txt").write_text(json.dumps(metadata), encoding="utf-8")


class ObservationCountsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.observations = self.root / "observations"

    def test_resets_and_winning_moves_are_attributed_to_the_previous_level(self) -> None:
        write_steps(self.observations, [
            frame(action="RESET"), frame(), frame(state="GAME_OVER"), frame(action="RESET"),
            frame(completed=1), frame(completed=1, action="ACTION6"),
            frame(completed=1, action="RESET"), frame(completed=2, state="WIN"),
        ])
        result = counter.analyze_observations(self.observations)
        self.assertEqual(result, ({"01": 2, "02": 2}, {"01": 4, "02": 3}, True))
        self.assertEqual(counter.format_counts(*result), "01/2/4, 02/2/3, WIN")

    def test_highlights_do_not_count_as_steps_or_attempts(self) -> None:
        write_steps(self.observations, [
            frame(action="RESET"), frame(action="highlight_pixel"), frame(),
            frame(state="GAME_OVER"), frame(state="GAME_OVER", action="highlight_pixel"),
            frame(action="RESET"), frame(completed=1),
            frame(completed=1, action=" highlight_pixel "), frame(completed=2, state="WIN"),
        ])
        self.assertEqual(
            counter.analyze_observations(self.observations),
            ({"01": 2, "02": 1}, {"01": 4, "02": 1}, True),
        )

    def test_initial_reset_is_not_an_action_or_extra_attempt(self) -> None:
        write_steps(self.observations, [frame(action="RESET")])
        self.assertEqual(counter.format_counts(*counter.analyze_observations(self.observations)), "01/1/0")

    def test_just_reached_level_has_one_attempt_and_zero_steps(self) -> None:
        write_steps(self.observations, [frame(), frame(completed=1)])
        result = counter.analyze_observations(self.observations)
        self.assertEqual(counter.format_counts(*result), "01/1/1, 02/1/0")

    def test_game_over_does_not_itself_start_another_attempt(self) -> None:
        write_steps(self.observations, [frame(), frame(state="GAME_OVER")])
        self.assertEqual(counter.format_counts(*counter.analyze_observations(self.observations)), "01/1/1")

    def test_numeric_step_order_and_unrelated_artifacts(self) -> None:
        frames = [frame(action="RESET"), *[frame() for _ in range(9)], frame(completed=1)]
        write_steps(self.observations, frames, padded=False)
        (self.observations / "step_0011_metadata.txt.tmp").write_text("unfinished", encoding="utf-8")
        (self.observations / "step_10_final.txt").write_text("not JSON", encoding="utf-8")
        (self.observations / "environment_description.md").write_text("description", encoding="utf-8")
        (self.observations / "step_0011_metadata.txt").mkdir()
        result = counter.analyze_observations(self.observations)
        self.assertEqual(counter.format_counts(*result), "01/1/10, 02/1/0")

    def test_empty_observations_are_not_mistaken_for_a_started_game(self) -> None:
        self.observations.mkdir()
        self.assertIsNone(counter.analyze_observations(self.observations))

    def test_missing_initial_step_gaps_and_duplicate_step_numbers_fail(self) -> None:
        for index, names in enumerate((
            ["step_0001_metadata.txt"],
            ["step_0000_metadata.txt", "step_0002_metadata.txt"],
            ["step_0000_metadata.txt", "step_0_metadata.txt"],
        )):
            with self.subTest(names=names):
                observations = self.root / str(index)
                observations.mkdir()
                for name in names:
                    (observations / name).write_text(json.dumps(frame()), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "consecutive from 0"):
                    counter.analyze_observations(observations)

    def test_invalid_json_and_non_object_metadata_have_useful_errors(self) -> None:
        self.observations.mkdir()
        path = self.observations / "step_0000_metadata.txt"
        for text in ("not JSON", "null", "[]", "true"):
            with self.subTest(text=text):
                path.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "step_0000_metadata.txt"):
                    counter.analyze_observations(self.observations)

    def test_invalid_level_counts_and_state_are_rejected(self) -> None:
        self.observations.mkdir()
        path = self.observations / "step_0000_metadata.txt"
        for key, value in (
            ("win_levels", 0), ("win_levels", True), ("win_levels", -1),
            ("levels_completed", -1), ("levels_completed", 3), ("levels_completed", "0"),
            ("levels_completed", 0.0), ("state", None),
        ):
            with self.subTest(key=key, value=value):
                path.write_text(json.dumps({**frame(), key: value}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, key):
                    counter.analyze_observations(self.observations)

    def test_invalid_progress_changes_and_terminal_sequences_are_rejected(self) -> None:
        sequences = [
            [frame(), frame(levels=3)],
            [frame(), frame(completed=2, state="WIN")],
            [frame(completed=1), frame(completed=0)],
            [frame(completed=2, state="WIN"), frame(completed=2, state="WIN")],
            [frame(completed=1), frame(completed=2)],
            [frame(), frame(completed=1, state="WIN")],
        ]
        for index, frames in enumerate(sequences):
            with self.subTest(frames=frames):
                observations = self.root / str(index)
                write_steps(observations, frames)
                with self.assertRaises(ValueError):
                    counter.analyze_observations(observations)

    def test_actions_after_initial_observation_require_string_ids(self) -> None:
        write_steps(self.observations, [frame(), frame()])
        path = self.observations / "step_0001_metadata.txt"
        for action in (None, [], {}, {"id": 1}):
            with self.subTest(action=action):
                path.write_text(json.dumps({**frame(), "action_input": action}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "action_input"):
                    counter.analyze_observations(self.observations)


class CommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.run_folder = self.root / "run with spaces"
        self.run_folder.mkdir()
        self.stdout = self.enterContext(redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(redirect_stderr(io.StringIO()))

    def write_log(self, *mappings: tuple[str, str]) -> None:
        prefix = "2026-09-09T17:30:34+0000 INFO [environment-owner] env-server.server: "
        for index, (alias, game) in enumerate(mappings):
            path = self.run_folder / alias / "server/server.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as log:
                log.write(f"{prefix}started session token=token-{index} requested_game_id={alias} game_id={game}\n")
                log.write(f"{prefix}applied action session=token-{index} action=not-parsed step_index=900\n")

    def test_new_layout_labels_and_counts_need_no_extracted_files_or_agent_logs(self) -> None:
        write_steps(self.run_folder / "alias-a/observations", [frame(), frame(completed=1), frame(completed=2, state="WIN")])
        write_steps(self.run_folder / "alias-b/observations", [frame()])
        self.write_log(("alias-a", "game10"), ("alias-b", "game2"))
        # Irrelevant generated files must not supply (or affect) the counts.
        for name in ("raw_actions.json", "raw_cost_tokens.json", "game10_scorecard.json"):
            (self.run_folder / name).write_text("not JSON", encoding="utf-8")
        before = {path: path.read_bytes() for path in self.run_folder.rglob("*") if path.is_file()}
        self.assertEqual(counter.main([str(self.run_folder)]), 0)
        self.assertEqual(self.stdout.getvalue(), "alias-b (game2): 01/1/0\nalias-a (game10): 01/1/1, 02/1/1, WIN\n")
        self.assertEqual(self.stderr.getvalue(), "")
        self.assertEqual({path: path.read_bytes() for path in self.run_folder.rglob("*") if path.is_file()}, before)

    def test_missing_log_keeps_complete_folder_alias_without_guessing_game_id(self) -> None:
        write_steps(self.run_folder / "1234abcde/observations", [frame()])
        self.assertEqual(counter.main([str(self.run_folder)]), 0)
        self.assertEqual(self.stdout.getvalue(), "1234abcde: 01/1/0\n")

    def test_precreated_empty_folder_is_reported_without_failing_other_games(self) -> None:
        (self.run_folder / "pending/observations").mkdir(parents=True)
        write_steps(self.run_folder / "started/observations", [frame()])
        self.assertEqual(counter.main([str(self.run_folder)]), 0)
        self.assertEqual(self.stdout.getvalue(), "pending: no observations\nstarted: 01/1/0\n")

    def test_no_observation_directories_has_a_clear_message(self) -> None:
        self.assertEqual(counter.main([str(self.run_folder)]), 0)
        self.assertIn("No observations directories found", self.stdout.getvalue())

    def test_corrupt_sequence_does_not_leave_partial_output(self) -> None:
        write_steps(self.run_folder / "first/observations", [frame()])
        observations = self.run_folder / "second/observations"
        observations.mkdir(parents=True)
        (observations / "step_0000_metadata.txt").write_text("broken JSON", encoding="utf-8")
        self.assertEqual(counter.main([str(self.run_folder)]), 1)
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertIn("second/observations/step_0000_metadata.txt", self.stderr.getvalue())

    def test_conflicting_game_labels_fail_without_mislabeling_results(self) -> None:
        write_steps(self.run_folder / "alias/observations", [frame()])
        self.write_log(("alias", "ls20"), ("alias", "bp35"))
        self.assertEqual(counter.main([str(self.run_folder)]), 1)
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertIn("multiple sessions", self.stderr.getvalue())

    def test_missing_run_folder_is_an_error(self) -> None:
        self.assertEqual(counter.main([str(self.run_folder / "missing")]), 1)
        self.assertIn("not a directory", self.stderr.getvalue())

    def test_cli_runs_from_another_directory_with_only_standard_library(self) -> None:
        write_steps(self.run_folder / "alias/observations", [frame(action="RESET")])
        result = subprocess.run(
            [sys.executable, "-S", str(ROOT / "analysis_scripts/arc3/count_level_attempts_dirs.py"), self.run_folder.name],
            cwd=self.root, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "alias: 01/1/0\n")


if __name__ == "__main__":
    unittest.main()
