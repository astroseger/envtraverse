from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis_scripts.agimaze_single import replay_single as replay


MAZE = "TUTORIAL/STAGE-01/0000.json"


class ReplayInputTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "actions with spaces.json"

    def write(self, payload) -> None:
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def test_name_key_is_inferred_without_a_fixed_schema(self) -> None:
        for key in ("environment_id", "name", "custom_name"):
            with self.subTest(key=key):
                actions = [{"id": " up ", "extra": 1}, {"id": "left"}]
                self.write([{"unrelated": "other", "actions": None},
                            {key: MAZE, "actions": actions, "run_folder": "/does/not/exist"}])
                saved = replay.load_saved_labyrinth(self.path, MAZE)
                self.assertEqual(saved.json_path, self.path)
                self.assertEqual(saved.name_key, key)
                self.assertEqual(saved.labyrinth_name, MAZE)
                self.assertEqual(saved.actions, tuple(actions))

    def test_only_top_level_exact_string_matches_are_considered(self) -> None:
        for payload in ([{"name": "prefix/" + MAZE, "actions": []}],
                        [{"name": MAZE + " ", "actions": []}],
                        [{"nested": {"name": MAZE}, "actions": [{"id": MAZE}]}],
                        [{"names": [MAZE], "actions": []}], []):
            with self.subTest(payload=payload):
                self.write(payload)
                with self.assertRaisesRegex(ValueError, "was not found"):
                    replay.load_saved_labyrinth(self.path, MAZE)

    def test_multiple_matching_keys_are_ambiguous_even_within_one_record(self) -> None:
        for records in ([{"name": MAZE, "environment_id": MAZE, "actions": []}],
                        [{"name": MAZE, "actions": []}, {"environment_id": MAZE, "actions": []}]):
            with self.subTest(records=records):
                self.write(records)
                with self.assertRaisesRegex(ValueError, "ambiguous.*matches keys"):
                    replay.load_saved_labyrinth(self.path, MAZE)

    def test_duplicate_matching_records_fail_even_when_actions_are_identical(self) -> None:
        self.write([{"environment_id": MAZE, "actions": []}] * 2)
        with self.assertRaisesRegex(ValueError, "duplicate.*records 1, 2"):
            replay.load_saved_labyrinth(self.path, MAZE)

    def test_input_must_be_a_list_of_dictionaries(self) -> None:
        for payload in ({}, None, "maze", [None], [{"name": MAZE, "actions": []}, 1]):
            with self.subTest(payload=payload):
                self.write(payload)
                with self.assertRaises(ValueError):
                    replay.load_saved_labyrinth(self.path, MAZE)

    def test_selected_record_requires_a_valid_action_list(self) -> None:
        for actions in (None, {}, "up", [None], [{}], [{"id": 1}], [{"id": " "}], [{"id": True}]):
            with self.subTest(actions=actions):
                self.write([{"name": MAZE, "actions": actions}])
                with self.assertRaisesRegex(ValueError, "record 1"):
                    replay.load_saved_labyrinth(self.path, MAZE)

    def test_empty_action_list_is_valid(self) -> None:
        self.write([{"name": MAZE, "actions": []}])
        self.assertEqual(replay.load_saved_labyrinth(self.path, MAZE).actions, ())

    def test_missing_malformed_json_and_blank_name_fail(self) -> None:
        with self.assertRaises(FileNotFoundError):
            replay.load_saved_labyrinth(self.path, MAZE)
        self.path.write_text("[", encoding="utf-8")
        with self.assertRaises(ValueError):
            replay.load_saved_labyrinth(self.path, MAZE)
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            replay.load_saved_labyrinth(self.path, " ")

    def test_loader_reads_only_supplied_json_and_changes_nothing(self) -> None:
        self.write([{"environment_id": MAZE, "actions": [], "run_folder": "/not/read"}])
        before = self.path.read_bytes()
        original_open = Path.open
        opened = []

        def checked_open(path, *args, **kwargs):
            self.assertEqual(path, self.path)
            opened.append(path)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", checked_open):
            replay.load_saved_labyrinth(self.path, MAZE)
        self.assertEqual(opened, [self.path])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_maze_must_be_an_exact_file_inside_packs(self) -> None:
        replay.validate_maze_path(MAZE)
        for name in ("TUTORIAL/STAGE-01", "missing.json", "../outside.json", "/tmp/maze.json"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                replay.validate_maze_path(name)


class SharedReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "raw_actions.json"
        self.path.write_text(json.dumps([{"environment_id": MAZE, "actions": [{"id": " up "}, {"id": "left"}]}]))
        self.saved = replay.load_saved_labyrinth(self.path, MAZE)
        self.process = Mock()
        self.start = self.enterContext(patch.object(replay, "start_server", return_value=self.process))
        self.stop = self.enterContext(patch.object(replay, "stop_server"))
        self.post = self.enterContext(patch.object(replay, "http_post_json"))
        self.options = {"host": "127.0.0.1", "port": 8000, "lang": "en", "timeout": 10.0}
        self.stdout = self.enterContext(redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(redirect_stderr(io.StringIO()))

    def test_initial_state_and_ordered_actions_use_one_session_and_sequential_indices(self) -> None:
        self.post.side_effect = [{"id": "test-session"}, {"done": False}, {"done": True}]
        steps = list(replay.replay_steps(self.saved, **self.options))
        self.assertEqual([(step.index, step.label, step.action) for step in steps],
                         [(0, "start", None), (1, "action_0000", "up"), (2, "action_0001", "left")])
        self.assertEqual(self.post.call_args_list, [
            call("http://127.0.0.1:8000/api/start", {"path": MAZE, "lang": "en", "client": "api", "agent": "replay_single"}),
            call("http://127.0.0.1:8000/api/step", {"id": "test-session", "action": "up", "seq": 1}),
            call("http://127.0.0.1:8000/api/step", {"id": "test-session", "action": "left", "seq": 2}),
        ])
        self.stop.assert_called_once_with(self.process)

    def test_empty_actions_yield_only_initial_state(self) -> None:
        saved = replay.SavedLabyrinthReplay(self.path, "name", MAZE, ())
        self.post.return_value = {"id": "test-session"}
        steps = list(replay.replay_steps(saved, **self.options))
        self.assertEqual(len(steps), 1)
        self.post.assert_called_once()
        self.stop.assert_called_once_with(self.process)

    def test_start_and_action_errors_always_stop_backend(self) -> None:
        failures = [([{"error": "bad_path"}], "Failed to start"),
                    ([{}], "session id"),
                    ([{"id": "session"}, {"error": "bad_action"}], "action_0000"),
                    ([{"id": "session"}, OSError("connection lost")], "connection lost")]
        for responses, message in failures:
            with self.subTest(responses=responses):
                self.stop.reset_mock()
                self.post.side_effect = responses
                with self.assertRaisesRegex((OSError, RuntimeError), message):
                    list(replay.replay_steps(self.saved, **self.options))
                self.stop.assert_called_once_with(self.process)

    def test_trailing_actions_after_completion_are_not_silently_ignored(self) -> None:
        self.post.side_effect = [{"id": "session"}, {"done": True}]
        with self.assertRaisesRegex(RuntimeError, "ended after 1 actions.*contains 2"):
            list(replay.replay_steps(self.saved, **self.options))
        self.assertEqual(self.post.call_count, 2)
        self.stop.assert_called_once_with(self.process)

    def test_closing_partially_consumed_replay_stops_backend(self) -> None:
        self.post.return_value = {"id": "session"}
        with closing(replay.replay_steps(self.saved, **self.options)) as steps:
            self.assertEqual(next(steps).index, 0)
        self.stop.assert_called_once_with(self.process)

    def test_text_cli_closes_generator_when_output_is_interrupted(self) -> None:
        self.post.return_value = {"id": "session"}
        with patch.object(replay, "print_replay_state", side_effect=KeyboardInterrupt):
            self.assertEqual(replay.main([str(self.path), MAZE]), 130)
        self.stop.assert_called_once_with(self.process)

    def test_text_cli_prints_maps_states_and_detected_name_key(self) -> None:
        self.post.side_effect = [
            {"id": "session", "map": "S-T\n", "formal": {"status": "success"}},
            {"done": False, "text": "moved", "map": "-ST"},
            {"done": True, "formal": {"events": ["exited"]}},
        ]
        self.assertEqual(replay.main([str(self.path), MAZE]), 0)
        for value in ("name_key: environment_id", "status: success", "map:\nS-T", "action_0001 seq=2 action=left",
                      "done: True", 'events: ["exited"]'):
            self.assertIn(value, self.stdout.getvalue())
        self.assertNotIn("results_file", self.stdout.getvalue())

    def test_invalid_input_is_rejected_before_launch(self) -> None:
        self.assertEqual(replay.main([str(self.path), "unknown"]), 1)
        self.start.assert_not_called()
        self.assertIn("was not found", self.stderr.getvalue())


class BackendLifecycleTest(unittest.TestCase):
    def test_launch_uses_current_interpreter_and_cleans_up_on_startup_failure(self) -> None:
        for error in (RuntimeError("not ready"), KeyboardInterrupt()):
            with self.subTest(error=type(error)), \
                    patch.object(replay.socket, "create_server"), \
                    patch.object(replay.subprocess, "Popen") as popen, \
                    patch.object(replay, "wait_until_ready", side_effect=error), \
                    patch.object(replay, "stop_server") as stop:
                with self.assertRaises(type(error)):
                    replay.start_server("127.0.0.1", 8000, 1)
                command = popen.call_args.args[0]
                self.assertEqual(command[:3], [sys.executable, "-B", "scripts/webui_server.py"])
                self.assertEqual(popen.call_args.kwargs["cwd"], replay.LABYRINTH_DIR)
                self.assertIn("--debug", command)
                stop.assert_called_once_with(popen.return_value)

    def test_occupied_port_does_not_start_or_contact_a_server(self) -> None:
        with patch.object(replay.socket, "create_server", side_effect=OSError("port in use")), \
                patch.object(replay.subprocess, "Popen") as popen, patch.object(replay, "http_get_json") as get:
            with self.assertRaisesRegex(OSError, "port in use"):
                replay.start_server("127.0.0.1", 8000, 1)
        popen.assert_not_called()
        get.assert_not_called()

    def test_stop_terminates_and_escalates_only_if_necessary(self) -> None:
        for slow in (False, True):
            with self.subTest(slow=slow):
                process = Mock()
                process.poll.return_value = None
                if slow:
                    process.wait.side_effect = [subprocess.TimeoutExpired("server", 2), 0]
                replay.stop_server(process)
                process.terminate.assert_called_once()
                self.assertEqual(process.kill.call_count, int(slow))
                self.assertEqual(process.wait.call_count, 1 + int(slow))

    def test_already_exited_backend_does_not_receive_signals(self) -> None:
        process = Mock()
        process.poll.return_value = 0
        replay.stop_server(process)
        process.terminate.assert_not_called()

    def test_readiness_detects_child_exit_before_and_after_http_success(self) -> None:
        for statuses in ([17], [None, 17]):
            with self.subTest(statuses=statuses), patch.object(replay, "http_get_json"):
                process = Mock(returncode=17)
                process.poll.side_effect = statuses
                with self.assertRaisesRegex(RuntimeError, "exited early with code 17"):
                    replay.wait_until_ready("http://127.0.0.1:8000", process, 1)

    def test_readiness_timeout_is_reported(self) -> None:
        process = Mock()
        process.poll.return_value = None
        with patch.object(replay.time, "monotonic", side_effect=[0, 0, 2]), \
                patch.object(replay.time, "sleep"), patch.object(replay, "http_get_json", side_effect=OSError("not ready")):
            with self.assertRaisesRegex(RuntimeError, "did not start.*not ready"):
                replay.wait_until_ready("http://127.0.0.1:8000", process, 1)


if __name__ == "__main__":
    unittest.main()
