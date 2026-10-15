from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from analysis_scripts import analyse_runs


class AnalyseRunsTest(unittest.TestCase):
    def test_interleaved_sessions_become_one_ordered_action_list(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            log_path = Path(temporary_dir) / "server.log"
            log_path.write_text(
                "\n".join(
                    [
                        self.session_line("token-b", "b/two.json"),
                        self.session_line("token-a", "a/one.json"),
                        self.action_line("token-a", {"id": "right"}, 1),
                        self.action_line("token-b", {"id": "up"}, 1),
                        self.action_line("token-a", {"id": "left"}, 2),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            actions_by_game = analyse_runs.actions_by_game_from_server_log(log_path)
            results = analyse_runs.action_results_for_games(actions_by_game)

            self.assertEqual(
                results,
                [
                    {"name": "b/two.json", "actions": [{"id": "up"}]},
                    {
                        "name": "a/one.json",
                        "actions": [{"id": "right"}, {"id": "left"}],
                    },
                ],
            )

    def test_non_contiguous_step_index_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            log_path = Path(temporary_dir) / "server.log"
            log_path.write_text(
                "\n".join(
                    [
                        self.session_line("token-a", "a/one.json"),
                        self.action_line("token-a", {"id": "up"}, 2),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "expected 1"):
                analyse_runs.actions_by_game_from_server_log(log_path)

    def test_replay_results_include_index_steps_limit_and_status(self) -> None:
        class StubReplayer:
            def replay(
                self,
                labyrinth_name: str,
                actions: list[analyse_runs.Action],
            ) -> analyse_runs.ReplayOutcome:
                return analyse_runs.ReplayOutcome(
                    max_steps=70 if labyrinth_name == "one.json" else 160,
                    solved=labyrinth_name == "one.json",
                )

        action_results = [
            {"name": "one.json", "actions": [{"id": "up"}, {"id": "left"}]},
            {"name": "two.json", "actions": [{"id": "down"}]},
        ]

        results = analyse_runs.replay_results(
            action_results,
            replayer=StubReplayer(),
        )

        self.assertEqual(
            results,
            [
                {
                    "name": "one.json",
                    "labyrinth_index": 0,
                    "steps": 2,
                    "max_steps": 70,
                    "solved": True,
                },
                {
                    "name": "two.json",
                    "labyrinth_index": 1,
                    "steps": 1,
                    "max_steps": 160,
                    "solved": False,
                },
            ],
        )

        with tempfile.TemporaryDirectory() as temporary_dir:
            run_folder = Path(temporary_dir)
            actions_path = analyse_runs.write_action_results(
                run_folder,
                action_results,
            )
            results_path = analyse_runs.write_replay_results(run_folder, results)

            self.assertEqual(actions_path.name, "actions.json")
            self.assertEqual(results_path.name, "results.json")
            self.assertEqual(
                json.loads(actions_path.read_text(encoding="utf-8")),
                action_results,
            )
            self.assertEqual(
                json.loads(results_path.read_text(encoding="utf-8")),
                results,
            )

    def test_local_labyrinth_engine_replay(self) -> None:
        actions = [
            {"id": action_id}
            for action_id in (
                "down",
                "left",
                "up",
                "left",
                "up",
                "right",
                "left",
                "down",
                "left",
            )
        ]

        outcome = analyse_runs.LabyrinthReplayer().replay(
            "TUTORIAL/STAGE-01/0000.json",
            actions,
        )

        self.assertEqual(
            outcome,
            analyse_runs.ReplayOutcome(max_steps=70, solved=True),
        )

    @staticmethod
    def session_line(token: str, game_id: str) -> str:
        return (
            "2026-09-04T10:33:38+0000 INFO [arcade-owner] env-server.server: "
            f"started session token={token} requested_game_id=requested-{token} "
            f"game_id={game_id}"
        )

    @staticmethod
    def action_line(token: str, action: dict[str, object], step_index: int) -> str:
        return (
            "2026-09-04T10:33:59+0000 INFO [arcade-owner] env-server.server: "
            f"applied action session={token} action={action!r} "
            f"step_index={step_index}"
        )


if __name__ == "__main__":
    unittest.main()
