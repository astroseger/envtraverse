from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

SERVER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_DIR))

import server
from environment_api import EnvironmentError, EnvironmentStartError, InvalidActionError
from environments.agimaze_set_v1 import environment_manager as sequence
from environments.agimaze_single import environment_manager as single


TUTORIAL = "TUTORIAL/STAGE-01/0000.json"
TRAINING = "TRAINING/S0-keys/STAGE-01/0000.json"
SOLUTION = ["down", "left", "up", "left", "up", "right", "left", "down", "left"]
START_RESPONSE = {
    "id": "maze-session",
    "text": "Game has started.",
    "map": "@..\n...",
    "inventory": {"key": False, "treasure": False},
    "actions": ["up", "down", "left", "right"],
    "task_meta": {"max_steps": 1, "board": {"n": 2, "m": 3}, "start": {"row": 0, "col": 0}},
}


class LabyrinthSetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "demo.json").write_text(json.dumps([
            {"path": TUTORIAL, "tutorial": True},
            {"path": TRAINING, "tutorial": False},
        ]), encoding="utf-8")
        self.enterContext(patch.object(sequence, "SETS_DIR", self.root))
        self.processes = []

        def make_process(*args, **kwargs):
            process = Mock()
            process.poll.return_value = None
            self.processes.append(process)
            return process

        self.popen = self.enterContext(patch.object(single.subprocess, "Popen", side_effect=make_process))
        self.enterContext(patch.object(single.EnvironmentManager, "_wait_until_ready"))
        self.get = self.enterContext(patch.object(single, "http_get_json", return_value={"description": "Rules."}))
        self.post = self.enterContext(patch.object(single, "http_post_json", return_value=START_RESPONSE))

    def start_set(self):
        self.manager = sequence.EnvironmentManager({"port": 5500, "seed": 42})
        self.addCleanup(self.manager.stop)
        return self.manager.start_environment("demo")

    def start_maze(self, game):
        self.post.return_value = START_RESPONSE
        return game.move({"id": "start"})

    def finish_maze(self, game, *, solved=True):
        self.post.return_value = {"text": "Finished.", "done": solved}
        if not solved:
            game.current_game.max_steps = 1
        return game.move({"id": "up"})

    def test_initial_message_and_fixed_description_without_starting_backend(self) -> None:
        result = self.start_set()
        self.assertEqual(result.observation.files, {"message.md": sequence.INITIAL_START_MESSAGE})
        self.assertFalse(result.observation.terminal)
        self.assertEqual(
            result.environment_description,
            (sequence.ENVIRONMENT_DIR / "environment_description.md").read_text(encoding="utf-8"),
        )
        self.assertIn("solve all labyrinths", result.environment_description)
        self.assertIsNone(self.manager.current_game)
        self.assertFalse(hasattr(self.manager, "environments"))
        self.assertFalse(hasattr(self.manager, "next_environment_id"))
        self.popen.assert_not_called()

    def test_waiting_only_accepts_start(self) -> None:
        self.start_set()
        game = self.manager
        for action in (None, [], {}, {"id": 1}, {"id": " "}, {"id": "up"}, {"id": "START"}):
            with self.subTest(action=action), self.assertRaises(InvalidActionError):
                game.move(action)
        self.popen.assert_not_called()
        observation = game.move({"id": " start "})
        self.assertFalse(observation.terminal)
        self.assertEqual(game.current_index, 0)

    def test_start_returns_current_description_state_and_tutorial_map(self) -> None:
        self.start_set()
        game = self.manager
        observation = self.start_maze(game)
        self.assertEqual(list(observation.files), ["state.txt", "labyrinth_description.md", "map.txt"])
        self.assertEqual(json.loads(observation.files["state.txt"]), {
            "text": START_RESPONSE["text"], "inventory": START_RESPONSE["inventory"], "labyrinth_index": 0,
        })
        self.assertEqual(observation.files["map.txt"], START_RESPONSE["map"] + "\n")
        self.assertIn("Rules.", observation.files["labyrinth_description.md"])
        self.assertIn("start position (row,col): (0,0)", observation.files["labyrinth_description.md"])
        self.assertTrue(observation.files["labyrinth_description.md"].endswith("\n"))
        self.post.assert_called_once_with("http://127.0.0.1:5500/api/start", {
            "path": TUTORIAL, "lang": "en", "seed": 42, "client": "api", "agent": "environment_manager",
        })
        self.assertEqual((game.current_game.step, game.current_game.max_steps), (1, 5))

    def test_ordinary_move_updates_tutorial_map_without_repeating_description(self) -> None:
        self.start_set()
        game = self.manager
        self.start_maze(game)
        self.post.return_value = {"text": "Moved.", "map": ".@.", "done": False}
        observation = game.move({"id": "right"})
        self.assertEqual(set(observation.files), {"state.txt", "map.txt"})
        self.assertEqual(observation.files["map.txt"], ".@.\n")
        self.assertEqual(json.loads(observation.files["state.txt"])["text"], "Moved.")
        self.assertFalse(observation.terminal)
        self.post.assert_called_with("http://127.0.0.1:5500/api/step", {
            "id": "maze-session", "action": "right", "seq": 1,
        })

    def test_start_is_rejected_while_playing(self) -> None:
        self.start_set()
        game = self.manager
        self.start_maze(game)
        self.post.reset_mock()
        with self.assertRaises(InvalidActionError):
            game.move({"id": "start"})
        self.post.assert_not_called()
        self.assertEqual(game.current_game.step, 1)

    def test_solved_and_failed_maze_both_wait_for_explicit_next_start(self) -> None:
        for solved, message in ((True, sequence.SOLVED_START_MESSAGE), (False, sequence.FAILED_START_MESSAGE)):
            with self.subTest(solved=solved):
                self.start_set()
                game = self.manager
                self.start_maze(game)
                process = game.current_game.process
                calls_before = self.popen.call_count
                observation = self.finish_maze(game, solved=solved)
                self.assertEqual(observation.files, {"message.md": message})
                self.assertFalse(observation.terminal)
                self.assertEqual(game.current_index, 1)
                self.assertIsNone(game.current_game)
                self.assertIs(game.phase, sequence.SequencePhase.AWAITING_START)
                self.assertEqual(self.popen.call_count, calls_before)
                process.terminate.assert_called_once()
                with self.assertRaises(InvalidActionError):
                    game.move({"id": "up"})
                observation = self.start_maze(game)
                self.assertEqual(set(observation.files), {"state.txt", "labyrinth_description.md"})
                self.assertEqual(json.loads(observation.files["state.txt"])["labyrinth_index"], 1)
                self.assertEqual(self.post.call_args.args[1]["path"], TRAINING)
                self.post.return_value = {"text": "Moved.", "map": "not an artifact", "done": False}
                self.assertEqual(set(game.move({"id": "up"}).files), {"state.txt"})

    def test_actual_five_times_budget_is_reset_for_each_maze(self) -> None:
        self.start_set()
        game = self.manager
        for index in range(2):
            self.start_maze(game)
            self.post.return_value = {"text": "Not finished.", "done": False}
            for _ in range(4):
                observation = game.move({"id": "up"})
                self.assertIn("state.txt", observation.files)
                self.assertFalse(observation.terminal)
            observation = game.move({"id": "up"})
            expected = sequence.FAILED_START_MESSAGE if index == 0 else sequence.FINAL_FAILED_MESSAGE
            self.assertEqual(observation.files, {"message.md": expected})
            self.assertEqual(observation.terminal, index == 1)
            self.assertEqual(self.post.call_args.args[1]["seq"], 5)

    def test_final_success_or_failure_is_terminal_and_closes_child(self) -> None:
        for solved, message in ((True, sequence.FINAL_SOLVED_MESSAGE), (False, sequence.FINAL_FAILED_MESSAGE)):
            with self.subTest(solved=solved):
                self.start_set()
                game = self.manager
                self.start_maze(game)
                self.finish_maze(game)
                self.start_maze(game)
                process = game.current_game.process
                observation = self.finish_maze(game, solved=solved)
                self.assertEqual(observation.files, {"message.md": message})
                self.assertTrue(observation.terminal)
                self.assertTrue(game.terminal)
                self.assertIsNone(game.current_game)
                for action in ({"id": "start"}, {"id": "up"}):
                    with self.assertRaisesRegex(InvalidActionError, "terminal"):
                        game.move(action)
                game.stop()
                game.stop()
                process.terminate.assert_called_once()

    def test_failed_start_closes_process_and_stays_waiting(self) -> None:
        self.start_set()
        game = self.manager
        self.post.return_value = {"error": "Unavailable"}
        with self.assertRaises(EnvironmentStartError):
            game.move({"id": "start"})
        self.processes[0].terminate.assert_called_once()
        self.assertEqual(game.current_index, 0)
        self.assertIsNone(game.current_game)
        self.assertIs(game.phase, sequence.SequencePhase.AWAITING_START)
        self.assertEqual(self.manager.parameters.port, 5500)
        self.start_maze(game)

    def test_failed_initial_artifact_closes_child_and_stays_waiting(self) -> None:
        self.start_set()
        game = self.manager
        with patch.object(game, "_current_files", side_effect=ValueError("Cannot serialize")):
            with self.assertRaisesRegex(ValueError, "Cannot serialize"):
                self.start_maze(game)
        self.processes[0].terminate.assert_called_once()
        self.assertIsNone(game.current_game)
        self.assertEqual(game.current_index, 0)
        self.assertIs(game.phase, sequence.SequencePhase.AWAITING_START)
        self.start_maze(game)

    def test_failed_move_does_not_advance_sequence_or_child(self) -> None:
        self.start_set()
        game = self.manager
        self.start_maze(game)
        self.post.return_value = {"error": "Unavailable"}
        with self.assertRaises(EnvironmentError):
            game.move({"id": "up"})
        self.assertEqual((game.current_index, game.current_game.step), (0, 1))
        self.assertFalse(game.terminal)
        self.processes[0].terminate.assert_not_called()

    def test_second_set_start_is_rejected_and_stop_closes_the_child(self) -> None:
        self.start_set()
        game = self.manager
        self.start_maze(game)
        with self.assertRaises(EnvironmentStartError):
            game.start_environment("demo")
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(game.current_index, 0)
        game.stop()
        game.stop()
        self.processes[0].terminate.assert_called_once()
        self.assertIsNone(game.current_game)
        with self.assertRaises(EnvironmentError):
            game.move({"id": "start"})

    def test_each_maze_gets_a_new_manager_on_the_same_port(self) -> None:
        self.start_set()
        self.start_maze(self.manager)
        first = self.manager.current_game
        self.finish_maze(self.manager)
        self.start_maze(self.manager)
        second = self.manager.current_game
        self.assertIsNot(first, second)
        self.assertEqual((first.port, second.port), (5500, 5500))
        self.assertIsNone(first.process)
        self.assertEqual(second.step, 1)

    def test_closing_waiting_game_does_not_start_backend(self) -> None:
        self.start_set()
        game = self.manager
        game.stop()
        game.stop()
        with self.assertRaisesRegex(EnvironmentError, "closed"):
            game.move({"id": "start"})
        self.popen.assert_not_called()


class SetDefinitionsTest(unittest.TestCase):
    def test_all_bundled_sets_reference_existing_maze_packs(self) -> None:
        paths = sorted(sequence.SETS_DIR.glob("*.json"))
        self.assertEqual(len(paths), 233)
        for path in paths:
            with self.subTest(set=path.stem):
                specs = sequence.load_labyrinth_set(path.stem)
                raw = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual([spec.path for spec in specs], [item["path"] for item in raw])
                for spec in specs:
                    maze_path = single.LABYRINTH_DIR / "packs" / spec.path
                    self.assertTrue(maze_path.is_file(), str(maze_path))
        self.assertEqual(len(sequence.load_labyrinth_set("set01")), 56)
        self.assertEqual(len(sequence.load_labyrinth_set("ts0_TRAINING_S0-keys_STAGE-01")), 8)

    def test_unknown_or_path_shaped_names_are_rejected(self) -> None:
        for name in ("", " ", "missing", "../set01", "set01.json", "/tmp/set01"):
            with self.subTest(name=name), self.assertRaises(EnvironmentStartError):
                manager = sequence.EnvironmentManager()
                self.addCleanup(manager.stop)
                manager.start_environment(name)

    def test_malformed_definitions_are_rejected(self) -> None:
        cases = (
            ("{", "valid JSON"), ("{}", "JSON list"), ("[]", "empty"),
            ('["maze"]', "must be an object"), ('[{}]', "non-empty path"),
            ('[{"path": " "}]', "non-empty path"), ('[{"path": 12}]', "non-empty path"),
            ('[{"path": "maze.json", "tutorial": "false"}]', "boolean"),
            ('[{"path": "maze.json", "tutorial": 1}]', "boolean"),
            ('[{"path": "maze.json", "tutorial": null}]', "boolean"),
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(sequence, "SETS_DIR", Path(directory)):
            for raw, message in cases:
                (Path(directory) / "bad.json").write_text(raw, encoding="utf-8")
                with self.subTest(raw=raw), self.assertRaisesRegex(EnvironmentStartError, message):
                    sequence.load_labyrinth_set("bad")

    def test_default_tutorial_flag_order_and_repeated_mazes(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(sequence, "SETS_DIR", Path(directory)):
            (Path(directory) / "demo.json").write_text(json.dumps([
                {"path": " b.json "}, {"path": "a.json", "tutorial": True}, {"path": "b.json"},
            ]), encoding="utf-8")
            self.assertEqual(sequence.load_labyrinth_set("demo"), (
                sequence.LabyrinthSpec("b.json", False), sequence.LabyrinthSpec("a.json", True),
                sequence.LabyrinthSpec("b.json", False),
            ))


class RealBackendServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # Preserve the vendored engine and packs; isolate the backend's win logs.
        backend = self.root / "labyrinth"
        (backend / "scripts").mkdir(parents=True)
        shutil.copyfile(single.LABYRINTH_DIR / "scripts/webui_server.py", backend / "scripts/webui_server.py")
        for name in ("src", "packs", "webui"):
            (backend / name).symlink_to(single.LABYRINTH_DIR / name, target_is_directory=True)
        self.enterContext(patch.object(single, "LABYRINTH_DIR", backend))
        self.enterContext(patch.dict(os.environ, {"PYTHONDONTWRITEBYTECODE": "1"}))
        sets = self.root / "sets"
        sets.mkdir()
        (sets / "demo.json").write_text(json.dumps([
            {"path": TUTORIAL, "tutorial": True}, {"path": TRAINING, "tutorial": False},
        ]), encoding="utf-8")
        self.enterContext(patch.object(sequence, "SETS_DIR", sets))
        self.manager = sequence.EnvironmentManager({"port": self.free_port()})
        self.game_server = server.GameServer(self.manager, game_id="demo", game_alias="public-alias")
        self.addCleanup(self.game_server.stop)
        self.client = server.create_app(self.game_server).test_client()

    @staticmethod
    def free_port():
        with socket.create_server(("127.0.0.1", 0)) as listener:
            return listener.getsockname()[1]

    @staticmethod
    def files(payload):
        return {item["name"]: base64.b64decode(item["content_base64"]).decode("utf-8")
                for item in payload["step"]["artifacts"]}

    def move(self, action):
        response = self.client.post("/game/action", json={"action": {"id": action}}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def test_real_win_then_budget_failure_through_generic_http_routes(self) -> None:
        started = self.client.post("/game/start", json={"game_id": "public-alias"})
        self.assertEqual(started.status_code, 200, started.get_json())
        initial = started.get_json()
        token = initial["session"]["token"]
        self.headers = {"X-Session-Token": token}
        game = self.manager
        self.assertIsNone(game.current_game)
        self.assertEqual(self.files(initial), {"message.md": sequence.INITIAL_START_MESSAGE})
        self.assertEqual(initial["step"]["step_index"], 0)

        observation = self.move("start")
        self.assertEqual(set(self.files(observation)), {"state.txt", "labyrinth_description.md", "map.txt"})
        self.assertEqual(game.current_game.max_steps, 350)
        first_process = game.current_game.process
        for action in SOLUTION:
            observation = self.move(action)
        self.assertEqual(self.files(observation), {"message.md": sequence.SOLVED_START_MESSAGE})
        self.assertFalse(observation["session"]["terminal"])
        self.assertIsNotNone(first_process.poll())
        self.assertIsNone(game.current_game)
        rejected = self.client.post("/game/action", json={"action": {"id": "up"}}, headers=self.headers)
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(self.game_server.step_index, 10)

        # The previous child has stopped, so the next maze reuses the same port.
        observation = self.move("start")
        self.assertEqual(set(self.files(observation)), {"state.txt", "labyrinth_description.md"})
        self.assertEqual(json.loads(self.files(observation)["state.txt"])["labyrinth_index"], 1)
        second_process = game.current_game.process
        game.current_game.max_steps = 1  # Exercise failure without hundreds of moves.
        observation = self.move("up")
        self.assertEqual(self.files(observation), {"message.md": sequence.FINAL_FAILED_MESSAGE})
        self.assertTrue(observation["session"]["terminal"])
        self.assertEqual(observation["step"]["step_index"], 12)
        self.assertIsNotNone(second_process.poll())
        description = self.client.get("/game/environment-description", headers=self.headers)
        self.assertEqual(description.get_json(), {"environment_description": initial["environment_description"]})
        last = self.client.get("/game/last-step", headers=self.headers)
        self.assertEqual(last.get_json(), observation)
        rejected = self.client.post("/game/action", json={"action": {"id": "start"}}, headers=self.headers)
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(self.game_server.step_index, 12)


if __name__ == "__main__":
    unittest.main()
