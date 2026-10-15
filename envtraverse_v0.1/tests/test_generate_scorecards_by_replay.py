from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from enum import Enum
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis_scripts.arc3 import generate_scorecards_by_replay as replay


class GameAction(Enum):
    RESET = 0
    ACTION1 = 1
    ACTION2 = 2
    ACTION3 = 3
    ACTION4 = 4
    ACTION5 = 5
    ACTION6 = 6
    ACTION7 = 7


class ReplayActionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = Mock(observation_space=object())
        self.card_data = {"score": 0, "environments": [{"id": "ls20", "runs": []}]}
        self.card = Mock()
        self.card.model_dump.return_value = self.card_data
        self.arc = Mock()
        self.arc.make.return_value = self.environment
        self.arc.close_scorecard.return_value = self.card
        self.factory = Mock(return_value=self.arc)
        self.enterContext(patch.dict(sys.modules, {
            "arc_agi": SimpleNamespace(Arcade=self.factory, OperationMode=SimpleNamespace(NORMAL="normal")),
            "arcengine": SimpleNamespace(GameAction=GameAction),
        }))
        self.enterContext(patch.dict(os.environ, {"OPERATION_MODE": "NORMAL"}))

    def test_replays_exact_order_and_only_explicit_resets_without_mutating_actions(self) -> None:
        actions = [
            {"id": "ACTION1"},
            {"id": "ACTION6", "data": {"x": " 12 ", "y": 34, "ignored": True}},
            {"id": "RESET"},
            {"id": " ACTION7 "},
        ]
        original = json.dumps(actions)
        self.environment.step.return_value = SimpleNamespace(state="GAME_OVER")
        self.assertEqual(replay.replay_actions("ls20-custom-version", actions), self.card_data)
        self.arc.make.assert_called_once_with(
            "ls20-custom-version", seed=0, render_mode=None, save_recording=False,
        )
        self.assertEqual(self.environment.method_calls, [
            call.step(GameAction.ACTION1, data=None),
            call.step(GameAction.ACTION6, data={"x": 12, "y": 34}),
            call.reset(),
            call.step(GameAction.ACTION7, data=None),
        ])
        self.assertEqual(json.dumps(actions), original)
        self.arc.close_scorecard.assert_called_once_with()
        self.card.model_dump.assert_called_once_with(mode="json")

    def test_zero_actions_still_create_and_finalize_a_scorecard(self) -> None:
        self.assertEqual(replay.replay_actions("ls20", []), self.card_data)
        self.environment.step.assert_not_called()
        self.environment.reset.assert_not_called()
        self.arc.close_scorecard.assert_called_once_with()

    def test_missing_environment_or_initial_frame_fails_and_closes_scorecard(self) -> None:
        for environment in (None, Mock(observation_space=None)):
            with self.subTest(environment=environment):
                self.arc.reset_mock()
                self.arc.make.return_value = environment
                with self.assertRaisesRegex(RuntimeError, "initialize environment"):
                    replay.replay_actions("ls20", [])
                self.arc.close_scorecard.assert_called_once_with()

    def test_step_failure_includes_index_and_does_not_continue(self) -> None:
        self.environment.step.side_effect = [object(), None, object()]
        with self.assertRaisesRegex(RuntimeError, "action 2.*no frame"):
            replay.replay_actions("ls20", [{"id": "ACTION1"}] * 3)
        self.assertEqual(self.environment.step.call_count, 2)
        self.arc.close_scorecard.assert_called_once_with()

    def test_reset_failure_includes_index_and_does_not_continue(self) -> None:
        self.environment.reset.return_value = None
        with self.assertRaisesRegex(RuntimeError, "action 1.*no frame"):
            replay.replay_actions("ls20", [{"id": "RESET"}, {"id": "ACTION1"}])
        self.environment.step.assert_not_called()
        self.arc.close_scorecard.assert_called_once_with()

    def test_bad_actions_fail_with_action_index(self) -> None:
        for action in (None, "ACTION1", {}, {"id": 1}, {"id": " "}, {"id": "ACTION99"}, {"id": "ACTION1", "data": {}}):
            with self.subTest(action=action):
                self.environment.reset_mock()
                with self.assertRaisesRegex(RuntimeError, "action 1"):
                    replay.replay_actions("ls20", [action])
                self.environment.step.assert_not_called()
                self.environment.reset.assert_not_called()

    def test_invalid_click_coordinates_are_not_silently_coerced(self) -> None:
        for data in (None, {}, {"x": 1}, {"x": True, "y": 2}, {"x": 1.5, "y": 2},
                     {"x": "1.5", "y": 2}, {"x": -1, "y": 2}, {"x": 64, "y": 2}):
            with self.subTest(data=data):
                with self.assertRaisesRegex(RuntimeError, "action 1.*ACTION6"):
                    replay.replay_actions("ls20", [{"id": "ACTION6", "data": data}])
        self.environment.step.assert_not_called()

    def test_missing_actions_or_scorecard_is_an_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "actions must be a list"):
            replay.replay_actions("ls20", None)
        self.factory.assert_not_called()
        self.arc.close_scorecard.return_value = None
        with self.assertRaisesRegex(RuntimeError, "no scorecard"):
            replay.replay_actions("ls20", [])

    def test_remote_modes_are_rejected_before_creating_arcade(self) -> None:
        for mode in ("COMPETITION", "online", " Competition "):
            with self.subTest(mode=mode), patch.dict(os.environ, {"OPERATION_MODE": mode}):
                with self.assertRaisesRegex(RuntimeError, "local mode"):
                    replay.replay_actions("ls20", [])
        self.factory.assert_not_called()

    def test_missing_optional_sdk_has_a_useful_error(self) -> None:
        with patch.dict(sys.modules, {"arc_agi": None}):
            with self.assertRaisesRegex(RuntimeError, "requires arc-agi and arcengine"):
                replay.replay_actions("ls20", [])


class ReplayCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.run_folder = self.root / "run with spaces"
        self.run_folder.mkdir(parents=True)
        self.raw_actions = self.run_folder / "raw_actions.json"
        self.stdout = self.enterContext(redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(redirect_stderr(io.StringIO()))

    def write_log(self, *messages: str) -> None:
        prefix = "2026-09-09T10:00:00+0000 INFO [environment-owner] env-server.server: "
        logs = {"alias-token": []}
        alias = "alias-token"
        for message in messages:
            if message.startswith("started session "):
                alias = message.split("requested_game_id=")[1].split()[0]
                logs.setdefault(alias, [])
            logs[alias].append(message)
        for alias, lines in logs.items():
            path = self.run_folder / alias / "server/server.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("".join(prefix + line + "\n" for line in lines), encoding="utf-8")

    def start_line(self, environment_id: str, token: str = "token") -> str:
        return f"started session token={token} requested_game_id=alias-{token} game_id={environment_id}"

    def test_extractor_is_rerun_and_outputs_use_ids_at_run_root_without_agent_logs(self) -> None:
        self.write_log(
            self.start_line("ls20"),
            "applied action session=token action={'id': 'ACTION1'} step_index=1",
            self.start_line("bp35", "empty"),
        )
        self.raw_actions.write_text("stale data", encoding="utf-8")
        cost_file = self.run_folder / "raw_cost_tokens.json"
        cost_file.write_text("leave unchanged", encoding="utf-8")
        card = {"score": 12.5, "environments": [{"id": "native-card-shape"}]}
        with patch.object(replay, "replay_actions", return_value=card) as play:
            for _ in range(2):
                self.assertEqual(replay.main([str(self.run_folder)]), 0)
        self.assertEqual(play.call_args_list, [
            call("bp35", []), call("ls20", [{"id": "ACTION1"}]),
        ] * 2)
        for game in ("ls20", "bp35"):
            self.assertEqual(json.loads((self.run_folder / f"{game}_scorecard.json").read_text()), card)
        self.assertEqual(len(json.loads(self.raw_actions.read_text())), 2)
        self.assertFalse((self.run_folder / "alias-token/run/agent.log").exists())
        self.assertEqual(cost_file.read_text(), "leave unchanged")
        self.assertFalse(list(self.run_folder.glob("*.tmp")))
        self.assertIn("Generated 2 scorecards; 0 failed.", self.stdout.getvalue())

    def test_extraction_failure_does_not_replay_stale_raw_actions(self) -> None:
        self.raw_actions.write_text("previous data", encoding="utf-8")
        with patch.object(replay, "replay_actions") as play:
            self.assertNotEqual(replay.main([str(self.run_folder)]), 0)
        play.assert_not_called()
        self.assertEqual(self.raw_actions.read_text(), "previous data")
        self.assertIn("extraction failed", self.stderr.getvalue())

    def test_failed_game_keeps_old_scorecard_and_other_games_continue(self) -> None:
        self.write_log(self.start_line("ls20"), self.start_line("bp35", "second"))
        previous = self.run_folder / "ls20_scorecard.json"
        previous.write_text("previous successful result", encoding="utf-8")
        with patch.object(replay, "replay_actions", side_effect=[{"score": 0}, RuntimeError("action 2 failed")]):
            self.assertEqual(replay.main([str(self.run_folder)]), 1)
        self.assertEqual(previous.read_text(), "previous successful result")
        self.assertEqual(json.loads((self.run_folder / "bp35_scorecard.json").read_text()), {"score": 0})
        self.assertIn("ls20: replay failed: action 2 failed", self.stderr.getvalue())
        self.assertIn("Generated 1 scorecards; 1 failed.", self.stdout.getvalue())

    def test_empty_run_is_successful_without_loading_sdk(self) -> None:
        self.write_log()
        with patch.object(replay, "replay_actions") as play:
            self.assertEqual(replay.main([str(self.run_folder)]), 0)
        play.assert_not_called()
        self.assertEqual(json.loads(self.raw_actions.read_text()), [])

    def test_malformed_records_unsafe_ids_and_duplicates_are_rejected(self) -> None:
        for records in ({}, [None], [{}], [{"environment_id": "../ls20"}],
                        [{"environment_id": r"..\ls20"}], [{"environment_id": "ls20"}] * 2):
            with self.subTest(records=records):
                self.raw_actions.write_text(json.dumps(records), encoding="utf-8")
                with self.assertRaises(ValueError):
                    replay.read_raw_actions(self.raw_actions)

    def test_nonfinite_scorecard_does_not_replace_previous_output(self) -> None:
        path = self.run_folder / "ls20_scorecard.json"
        path.write_text("previous", encoding="utf-8")
        with self.assertRaises(ValueError):
            replay.write_scorecard(path, {"score": float("nan")})
        self.assertEqual(path.read_text(), "previous")
        self.assertFalse(list(self.run_folder.glob("*.tmp")))

    def test_uses_same_interpreter_and_absolute_extractor_path(self) -> None:
        self.raw_actions.write_text("[]", encoding="utf-8")
        with patch.object(replay.subprocess, "run") as run:
            self.assertEqual(replay.main([str(self.run_folder)]), 0)
        run.assert_called_once_with(
            [sys.executable, str(replay.EXTRACTOR_PATH), str(self.run_folder)], check=True,
        )

    def test_cli_with_real_host_sdk_offline(self) -> None:
        if importlib.util.find_spec("arc_agi") is None or importlib.util.find_spec("arcengine") is None:
            self.skipTest("optional host ARC libraries are not installed")
        game_dir = self.root / "environment_files" / "zz00" / "test"
        game_dir.mkdir(parents=True)
        (game_dir / "metadata.json").write_text(json.dumps({
            "game_id": "zz00-test", "class_name": "Zz00", "baseline_actions": [1],
        }), encoding="utf-8")
        (game_dir / "zz00.py").write_text(
            "from arcengine import ARCBaseGame, GameAction, Level\n"
            "class Zz00(ARCBaseGame):\n"
            "    def __init__(self, seed=0):\n"
            "        super().__init__('zz00', [Level(grid_size=(4, 4))], seed=seed)\n"
            "    def step(self):\n"
            "        if self.action.id == GameAction.ACTION1:\n"
            "            self.next_level()\n"
            "        self.complete_action()\n",
            encoding="utf-8",
        )
        environment = dict(
            os.environ, OPERATION_MODE="OFFLINE", ARC_API_KEY="offline-test-key",
            ARC_BASE_URL="http://127.0.0.1:1", ENVIRONMENTS_DIR=str(self.root / "environment_files"),
            RECORDINGS_DIR=str(self.root / "recordings"),
        )
        for action_count in (0, 1):
            self.write_log(self.start_line("zz00"), *(
                ["applied action session=token action={'id': 'ACTION1'} step_index=1"] if action_count else []
            ))
            result = subprocess.run(
                [sys.executable, str(Path(replay.__file__).resolve()), self.run_folder.name],
                cwd=self.root, env=environment, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            card = json.loads((self.run_folder / "zz00_scorecard.json").read_text())
            self.assertEqual(card["total_actions"], action_count)
            self.assertEqual(card["total_levels_completed"], action_count)
            self.assertEqual(card["total_levels"], 1)
            self.assertEqual(card["environments"][0]["id"], "zz00-test")
            self.assertEqual(len(card["environments"][0]["runs"]), 1)
            self.assertIsNone(card.get("api_key"))
        self.assertFalse((self.root / "recordings").exists())


if __name__ == "__main__":
    unittest.main()
