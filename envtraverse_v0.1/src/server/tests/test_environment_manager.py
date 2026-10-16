from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import subprocess
import unittest
from unittest.mock import Mock, patch


SERVER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_DIR))

from environments.agimaze_single import environment_manager
from environment_api import EnvironmentError, EnvironmentStartError


START_RESPONSE = {
    "id": "labyrinth-session",
    "map": "@..\n...",
    "text": "Game has started.",
    "inventory": {"key": False, "treasure": False},
    "actions": ["up", "down", "left", "right"],
    "info": {"rivers": 0, "pits_cycles": 0, "pits_cycle_len": 0},
    "task_meta": {
        "max_steps": 70,
        "board": {"n": 2, "m": 3},
        "start": {"row": 0, "col": 0},
    },
}


class BackendTimeoutTest(unittest.TestCase):
    def test_backend_get_and_post_allow_one_hour_for_results(self) -> None:
        with patch.object(
            environment_manager.urllib.request, "urlopen",
            side_effect=lambda *args, **kwargs: io.BytesIO(b'{"ok": true}'),
        ) as open_url:
            self.assertEqual(environment_manager.http_get_json("http://backend/description"), {"ok": True})
            self.assertEqual(environment_manager.http_post_json("http://backend/start", {}), {"ok": True})
        self.assertEqual([call.kwargs["timeout"] for call in open_url.call_args_list], [3600.0, 3600.0])

    def test_unresponsive_readiness_probe_does_not_inherit_the_hour_long_wait(self) -> None:
        elapsed = 0.0
        process = Mock(poll=Mock(return_value=None))
        manager = environment_manager.EnvironmentManager()

        def open_url(url, *, timeout):
            nonlocal elapsed
            elapsed += timeout
            raise TimeoutError("backend is not responding")

        def sleep(seconds):
            nonlocal elapsed
            elapsed += seconds

        with (
            patch.object(environment_manager.time, "monotonic", side_effect=lambda: elapsed),
            patch.object(environment_manager.time, "sleep", side_effect=sleep),
            patch.object(environment_manager.urllib.request, "urlopen", side_effect=open_url),
        ):
            with self.assertRaisesRegex(RuntimeError, "Labyrinth server did not start"):
                manager._wait_until_ready("http://backend", process, timeout_s=3.0)
        self.assertGreaterEqual(elapsed, 3.0)
        self.assertLess(elapsed, 4.0)


class EnvironmentManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.process = Mock()
        self.process.poll.return_value = None
        self.popen_patch = patch.object(
            environment_manager.subprocess,
            "Popen",
            return_value=self.process,
        )
        self.ready_patch = patch.object(
            environment_manager.EnvironmentManager,
            "_wait_until_ready",
        )
        self.get_patch = patch.object(
            environment_manager,
            "http_get_json",
            return_value={"description": "General labyrinth rules."},
        )
        self.post_patch = patch.object(
            environment_manager,
            "http_post_json",
            return_value=START_RESPONSE,
        )
        self.mock_popen = self.popen_patch.start()
        self.mock_ready = self.ready_patch.start()
        self.mock_get = self.get_patch.start()
        self.mock_post = self.post_patch.start()
        self.manager = environment_manager.EnvironmentManager()

    def start_game(self, game_id):
        return self.manager.start_environment(game_id)

    def tearDown(self) -> None:
        self.manager.stop()
        self.post_patch.stop()
        self.get_patch.stop()
        self.ready_patch.stop()
        self.popen_patch.stop()

    def test_start_is_one_labyrinth_and_description_is_not_an_artifact(self) -> None:
        game = "TRAINING/S0-keys/STAGE-01/0000.json"
        result = self.start_game(game)

        self.assertEqual(self.manager.port, environment_manager.DEFAULT_PORT)
        self.assertFalse(result.observation.terminal)
        self.assertEqual(list(result.observation.files), ["state.txt"])
        self.assertEqual(
            json.loads(result.observation.files["state.txt"]),
            {
                "inventory": {"key": False, "treasure": False},
                "text": "Game has started.",
            },
        )
        self.mock_post.assert_called_once()
        self.assertEqual(self.mock_post.call_args.args[1]["path"], game)

    def test_environment_description_is_in_start_result(self) -> None:
        result = self.start_game("TRAINING/S0-keys/STAGE-01/0000.json")

        description = result.environment_description

        self.assertIn("General labyrinth rules.", description)
        self.assertIn("board size: n=2 rows, m=3 cols", description)
        self.assertIn("start position (row,col): (0,0)", description)
        self.assertIn('{"id": "up"}', description)

    def test_move_returns_raw_text_and_terminal_state(self) -> None:
        result = self.start_game("TRAINING/S0-keys/STAGE-01/0000.json")
        self.mock_post.return_value = {"text": "You won.", "inventory": {"treasure": True}, "done": True}
        observation = self.manager.move(action={"id": "up"})
        self.assertTrue(observation.terminal)
        self.assertEqual(json.loads(observation.files["state.txt"])["text"], "You won.")

    def test_failed_start_terminates_process_and_cannot_be_retried(self) -> None:
        self.mock_post.return_value = {"error": "Unknown game"}
        with self.assertRaises(EnvironmentStartError):
            self.manager.start_environment("missing.json")
        self.process.terminate.assert_called_once()
        self.assertIsNone(self.manager.process)
        with self.assertRaises(EnvironmentStartError):
            self.manager.start_environment("another.json")
        self.mock_popen.assert_called_once()

    def test_failed_move_does_not_advance_engine_sequence(self) -> None:
        result = self.start_game("TRAINING/S0-keys/STAGE-01/0000.json")
        self.mock_post.return_value = {"error": "Step unavailable"}
        with self.assertRaises(EnvironmentError):
            self.manager.move({"id": "up"})
        self.assertEqual(self.manager.step, 1)

    def test_stop_cleans_up_once(self) -> None:
        self.start_game("TRAINING/S0-keys/STAGE-01/0000.json")
        game = self.manager
        game.stop()
        game.stop()
        self.manager.stop()
        self.manager.stop()
        self.process.terminate.assert_called_once()
        self.assertIsNone(game.process)

    def test_constructor_and_stop_before_start_do_not_launch_backend(self) -> None:
        self.mock_popen.assert_not_called()
        with self.assertRaises(EnvironmentError):
            self.manager.move({"id": "up"})
        self.manager.stop()
        self.manager.stop()
        with self.assertRaises(EnvironmentStartError):
            self.manager.start_environment("first.json")
        self.mock_popen.assert_not_called()

    def test_second_start_is_rejected_without_changing_the_game(self) -> None:
        self.start_game("first.json")
        with self.assertRaises(EnvironmentStartError):
            self.start_game("second.json")
        self.mock_popen.assert_called_once()
        self.assertEqual(self.manager.session_id, START_RESPONSE["id"])
        self.process.terminate.assert_not_called()
        self.mock_post.return_value = {"text": "Moved.", "done": False}
        self.manager.move({"id": "right"})
        self.mock_post.assert_called_with(
            "http://127.0.0.1:5000/api/step",
            {"id": START_RESPONSE["id"], "action": "right", "seq": 1},
        )

    def test_stop_kills_a_process_that_does_not_terminate(self) -> None:
        self.start_game("first.json")
        game = self.manager
        self.process.wait.side_effect = [subprocess.TimeoutExpired("engine", 2.0), None]
        game.stop()
        game.stop()
        self.process.terminate.assert_called_once()
        self.process.kill.assert_called_once()
        self.assertEqual(self.process.wait.call_count, 2)


if __name__ == "__main__":
    unittest.main()
