from __future__ import annotations

from contextlib import redirect_stdout
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import requests
from werkzeug.serving import make_server, WSGIRequestHandler


ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = ROOT / "src/agents/test_arc3_agent_v1_03_wstop_wreset_wlayout/agent"

# These variants deliberately share module names. Restore the import cache so
# this test file does not select a different agent for unrelated tests.
with patch.dict(sys.modules), patch.object(sys, "path", [
    str(AGENT_SRC / "client"), str(AGENT_SRC), str(ROOT / "src/server"), *sys.path,
]):
    for name in ("client", "client_storage", "session_inspector", "server", "environment_api", "environment_loader", "artifacts"):
        sys.modules.pop(name, None)
    client = importlib.import_module("client")
    storage = importlib.import_module("client_storage")
    inspector = importlib.import_module("session_inspector")
    server = importlib.import_module("server")
    api = importlib.import_module("environment_api")


DESCRIPTION = "ARC3 description: metadata.txt and final frames.\n"


def observation(*, completed=0, state="NOT_FINISHED", terminal=False, frames=True, label="frame"):
    files = {"metadata.txt": json.dumps({
        "state": state, "levels_completed": completed, "win_levels": 2,
        "available_actions": ["ACTION1", "ACTION2"],
        # RESET detection must use the actual response action, even when the
        # underlying frame does not label itself as a RESET.
        "action_input": {"id": "ACTION1", "data": {}},
    }, indent=2)}
    if frames:
        files.update({
            "final.txt": f"final {label}\n",
            "final.png": b"\x89PNG\x00\xff" + label.encode(),
            "intermediate_00.txt": f"intermediate {label}\n",
            "intermediate_00.png": b"\x89PNG\x00\xffintermediate" + label.encode(),
        })
    return api.Observation(files, terminal=terminal)


class ScriptedArc3Manager:
    """ARC3-shaped observations through the real server and artifact encoder."""

    def __init__(self):
        self.initial = observation(label="start")
        self.next_observation = observation()
        self.actions = []

    def start_environment(self, game_id):
        return api.StartResult(DESCRIPTION, self.initial)

    def move(self, action):
        self.actions.append(action)
        return self.next_observation

    def stop(self):
        pass


class QuietRequestHandler(WSGIRequestHandler):
    def log_request(self, code="-", size="-"):
        pass


class Arc3AgentLayoutTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="arc3 layout ")))
        self.client_dir = self.root / "client"
        shutil.copytree(AGENT_SRC / "client", self.client_dir, ignore=shutil.ignore_patterns("__pycache__"))
        self.observations = self.root / "observations"
        self.observations.mkdir()
        storage.initialize_client_storage(self.client_dir, self.observations, server_url="http://env-server:8879")
        self.state_path = self.client_dir / ".client_session.json"
        self.enterContext(patch.multiple(
            client,
            CLIENT_DIR=self.client_dir,
            OBSERVATIONS_DIR=self.observations,
            CLIENT_STATE_PATH=self.state_path,
            CLIENT_LOCK_PATH=self.client_dir / ".client.lock",
            ENVIRONMENT_DESCRIPTION_PATH=self.observations / "environment_description.md",
        ))
        self.enterContext(patch.multiple(
            inspector, CLIENT_STATE_PATH=self.state_path, OBSERVATIONS_DIR=self.observations,
        ))
        self.manager = ScriptedArc3Manager()
        self.game = server.GameServer(self.manager, game_id="arc3-test", game_alias="alias")
        self.http = server.create_app(self.game).test_client()
        self.post = self.enterContext(patch.object(client.HTTP_SESSION, "post", side_effect=self.http_post))
        self.enterContext(patch.object(client.HTTP_SESSION, "get", side_effect=self.http_get))
        self.addCleanup(self.game.stop)

    @staticmethod
    def response(wrapped):
        result = requests.Response()
        result.status_code = wrapped.status_code
        result._content = wrapped.data
        return result

    def http_post(self, url, *, json, headers, timeout):
        self.assert_readonly()
        return self.response(self.http.post(urlsplit(url).path, json=json, headers=headers))

    def http_get(self, url, *, headers, timeout):
        self.assert_readonly()
        return self.response(self.http.get(urlsplit(url).path, headers=headers))

    def cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output), patch.object(sys, "argv", ["client.py", *args]), \
                patch.object(client, "ignore_interrupt_and_term_signals"):
            code = client.main()
        return code, output.getvalue()

    def successful_cli(self, *args):
        code, output = self.cli(*args)
        self.assertEqual(code, 0, output)
        self.assert_reported_files_exist(output)
        self.assert_readonly()
        return output

    def start(self):
        return self.successful_cli("start", "alias")

    def move(self, action="ACTION1", **kwargs):
        self.manager.next_observation = observation(**kwargs)
        return self.successful_cli("move", json.dumps({"id": action}))

    def advance_without_client(self, action="ACTION1", **kwargs):
        self.manager.next_observation = observation(**kwargs)
        response = self.http.post("/game/action", json={"action": {"id": action}}, headers={
            "X-Session-Token": self.state()["token"],
        })
        self.assertEqual(response.status_code, 200)

    def state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def assert_position(self, global_step, level, attempt, local_step):
        state = self.state()
        self.assertEqual(tuple(state[key] for key in (
            "step_index", "current_level", "current_attempt", "current_attempt_step",
        )), (global_step, level, attempt, local_step))

    def assert_readonly(self):
        for root in (self.client_dir, self.observations):
            for path in (root, *root.rglob("*")):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o555 if path.is_dir() else 0o444, str(path))

    def reported_paths(self, output):
        directory = None
        paths = []
        for line in output.splitlines():
            if line.startswith("path: "):
                directory = Path(line.removeprefix("path: "))
            elif line.startswith("  "):
                self.assertIsNotNone(directory)
                paths.append(directory / line.strip())
        return paths

    def assert_reported_files_exist(self, output):
        paths = self.reported_paths(output)
        self.assertTrue(paths, output)
        for path in paths:
            self.assertTrue(path.is_file(), str(path))
            self.assertNotIn("actions", path.parts)
            self.assertFalse(path.name.endswith("_metadata.txt"), str(path))
        self.assertFalse((self.observations / "actions").exists())

    def initial_paths(self, directory):
        return {self.observations / directory / name for name in (
            "initial_metadata.json", "initial_frame.png", "initial_frame.txt",
        )}

    def step_paths(self, directory, step):
        return {self.observations / directory / f"step_{step:04d}_{name}" for name in (
            "metadata.json", "final.png", "final.txt", "intermediate_00.png", "intermediate_00.txt",
        )}

    def test_start_saves_initial_names_and_description_and_commits_position(self):
        output = self.start()
        expected = self.initial_paths("level_01_attempt_01")
        self.assertEqual(set(self.reported_paths(output)), expected)
        self.assertEqual(set(self.observations.rglob("*")), {
            *expected, self.observations / "level_01_attempt_01", client.ENVIRONMENT_DESCRIPTION_PATH,
        })
        self.assertEqual(client.ENVIRONMENT_DESCRIPTION_PATH.read_text(encoding="utf-8"), DESCRIPTION)
        self.assertIn(f"environment description: {client.ENVIRONMENT_DESCRIPTION_PATH}", output)
        self.assert_position(0, 1, 1, 0)
        metadata = self.observations / "level_01_attempt_01/initial_metadata.json"
        self.assertEqual(metadata.read_text(), self.manager.initial.files["metadata.txt"])
        self.assertEqual(inspector.inspect_sessions().to_dict(), {
            "step_index": 0, "terminal": False, "is_game_over": False,
        })

    def test_ordinary_step_retains_all_frames_with_local_names(self):
        self.start()
        output = self.move(label="ordinary")
        self.assertEqual(set(self.reported_paths(output)), self.step_paths("level_01_attempt_01", 1))
        final = self.observations / "level_01_attempt_01/step_0001_final.png"
        self.assertEqual(final.read_bytes(), self.manager.next_observation.files["final.png"])
        self.assert_position(1, 1, 1, 1)
        output = self.successful_cli("status")
        self.assertEqual(set(self.reported_paths(output)), self.step_paths("level_01_attempt_01", 1))

    def test_reset_opens_new_attempt_and_local_numbering_restarts(self):
        self.start()
        self.move(state="GAME_OVER")
        self.assertTrue(inspector.inspect_sessions().is_game_over)
        output = self.move("RESET", label="reset")
        self.assertEqual(set(self.reported_paths(output)), self.initial_paths("level_01_attempt_02"))
        self.assert_position(2, 1, 2, 0)
        self.assertFalse(inspector.inspect_sessions().is_game_over)
        self.assertEqual(inspector.inspect_sessions().step_index, 2)
        self.assertFalse((self.observations / "level_01_attempt_01/step_0002_metadata.json").exists())
        output = self.move(label="after-reset")
        self.assertEqual(set(self.reported_paths(output)), self.step_paths("level_01_attempt_02", 1))
        self.assert_position(3, 1, 2, 1)
        self.assertFalse((self.observations / "level_01_attempt_02/step_0003_metadata.json").exists())

    def test_level_transition_duplicates_final_frame_and_metadata(self):
        self.start()
        self.move(state="GAME_OVER")
        self.move("RESET")
        output = self.move(completed=1, label="next-level")
        expected = self.step_paths("level_01_attempt_02", 1) | self.initial_paths("level_02_attempt_01")
        self.assertEqual(set(self.reported_paths(output)), expected)
        self.assert_position(3, 2, 1, 0)
        old = self.observations / "level_01_attempt_02"
        new = self.observations / "level_02_attempt_01"
        for source, target in (
            ("step_0001_metadata.json", "initial_metadata.json"),
            ("step_0001_final.png", "initial_frame.png"),
            ("step_0001_final.txt", "initial_frame.txt"),
        ):
            self.assertEqual((old / source).read_bytes(), (new / target).read_bytes())
        self.assertEqual(set(new.iterdir()), self.initial_paths("level_02_attempt_01"))
        output = self.successful_cli("status")
        self.assertEqual(set(self.reported_paths(output)), self.initial_paths("level_02_attempt_01"))
        output = self.move(completed=1)
        self.assertEqual(set(self.reported_paths(output)), self.step_paths("level_02_attempt_01", 1))
        self.assert_position(4, 2, 1, 1)

    def test_win_stays_in_last_level_and_refuses_more_actions(self):
        self.start()
        self.move(completed=1)
        output = self.move(completed=2, state="WIN", terminal=True)
        self.assertEqual(set(self.reported_paths(output)), self.step_paths("level_02_attempt_01", 1))
        self.assert_position(2, 2, 1, 1)
        self.assertFalse((self.observations / "level_03_attempt_01").exists())
        self.assertTrue(inspector.inspect_sessions().terminal)
        self.assertFalse(inspector.inspect_sessions().is_game_over)
        posts = self.post.call_count
        code, output = self.cli("move", '{"id":"RESET"}')
        self.assertEqual(code, 1)
        self.assertIn("terminal", output)
        self.assertEqual(self.post.call_count, posts)

    def test_terminal_game_over_is_still_visible_to_inspector(self):
        self.start()
        self.move(state="GAME_OVER", terminal=True)
        self.assertEqual(inspector.inspect_sessions().to_dict(), {
            "step_index": 1, "terminal": True, "is_game_over": True,
        })

    def test_metadata_only_observations_do_not_print_nonexistent_images(self):
        self.manager.initial = observation(frames=False)
        output = self.start()
        self.assertEqual(self.reported_paths(output), [self.observations / "level_01_attempt_01/initial_metadata.json"])
        output = self.move(completed=1, frames=False)
        self.assertEqual(set(self.reported_paths(output)), {
            self.observations / "level_01_attempt_01/step_0001_metadata.json",
            self.observations / "level_02_attempt_01/initial_metadata.json",
        })
        output = self.successful_cli("status")
        self.assertEqual(self.reported_paths(output), [self.observations / "level_02_attempt_01/initial_metadata.json"])

    def test_status_recovers_unsaved_action_without_replaying_it(self):
        self.start()
        self.advance_without_client(label="lost-response")
        self.assert_position(0, 1, 1, 0)
        output = self.successful_cli("status")
        self.assertIn("Recovered the last server step", output)
        self.assertEqual(set(self.reported_paths(output)), self.step_paths("level_01_attempt_01", 1))
        self.assert_position(1, 1, 1, 1)
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(len(self.manager.actions), 1)

    def interrupted_save(self, *, reset, at_commit):
        self.start()
        if reset:
            self.move(state="GAME_OVER")
        old_state = self.state()
        self.manager.next_observation = observation(completed=0 if reset else 1, label="interrupted")
        directory = "level_01_attempt_02" if reset else "level_02_attempt_01"
        target = self.observations / directory / "initial_frame.png"
        real_write = client.write_readonly_file

        def write_then_interrupt(path, content):
            real_write(path, content)
            if path == target:
                raise OSError("simulated interruption during artifact save")

        failure = patch.object(client, "write_client_state", side_effect=OSError("simulated interruption before commit")) \
            if at_commit else patch.object(client, "write_readonly_file", side_effect=write_then_interrupt)
        with failure:
            code, output = self.cli("move", json.dumps({"id": "RESET" if reset else "ACTION1"}))
        self.assertEqual(code, 1, output)
        self.assertEqual(self.state(), old_state)
        self.assertTrue(target.is_file())
        # Inspection must ignore the uncommitted newer directory/files.
        self.assertEqual(inspector.inspect_sessions().is_game_over, reset)
        action_count = len(self.manager.actions)
        output = self.successful_cli("status")
        expected = self.initial_paths(directory)
        if not reset:
            expected |= self.step_paths("level_01_attempt_01", 1)
        self.assertEqual(set(self.reported_paths(output)), expected)
        self.assertEqual(len(self.manager.actions), action_count)
        self.assert_position(2 if reset else 1, 1 if reset else 2, 2 if reset else 1, 0)
        committed_state = self.state()
        self.successful_cli("status")
        self.assertEqual(self.state(), committed_state)
        self.assertEqual({p.name for p in self.observations.iterdir() if p.is_dir()}, {
            "level_01_attempt_01", directory,
        })

    def test_reset_recovery_after_partial_file_save(self):
        self.interrupted_save(reset=True, at_commit=False)

    def test_reset_recovery_after_all_files_but_before_session_commit(self):
        self.interrupted_save(reset=True, at_commit=True)

    def test_level_transition_recovery_after_partial_duplicate(self):
        self.interrupted_save(reset=False, at_commit=False)

    def test_level_transition_recovery_after_both_copies_but_before_commit(self):
        self.interrupted_save(reset=False, at_commit=True)

    def test_move_after_recovering_reset_prints_recovered_and_new_files(self):
        self.start()
        self.move(state="GAME_OVER")
        self.advance_without_client("RESET")
        output = self.move(label="after-recovery")
        self.assertEqual(set(self.reported_paths(output)),
                         self.initial_paths("level_01_attempt_02") | self.step_paths("level_01_attempt_02", 1))
        self.assert_position(3, 1, 2, 1)
        self.assertEqual([action["id"] for action in self.manager.actions], ["ACTION1", "RESET", "ACTION1"])

    def test_invalid_metadata_aborts_recovery_without_changing_position(self):
        self.start()
        self.manager.next_observation = api.Observation({"metadata.txt": "not JSON"})
        state = self.state()
        code, _output = self.cli("move", '{"id":"ACTION1"}')
        self.assertEqual(code, 1)
        code, _output = self.cli("status")
        self.assertEqual(code, 1)
        self.assertEqual(self.state(), state)
        self.assertEqual(set((self.observations / "level_01_attempt_01").iterdir()), self.initial_paths("level_01_attempt_01"))

    def test_invalid_saved_layout_position_is_rejected(self):
        self.start()
        state = self.state()
        for key, value in (("current_level", 0), ("current_attempt", -1), ("current_attempt_step", True)):
            with self.subTest(key=key):
                storage.write_readonly_file(self.state_path, json.dumps({**state, key: value}).encode())
                with self.assertRaisesRegex(RuntimeError, key):
                    client.read_client_state()

    def test_status_reseals_attempt_directory_after_interrupted_write_window(self):
        self.start()
        self.move(state="GAME_OVER")
        self.move("RESET")
        for directory in self.observations.glob("level_*_attempt_*"):
            directory.chmod(0o755)
        self.successful_cli("status")
        self.assert_readonly()

    def test_description_command_preserves_description_at_observations_root(self):
        self.start()
        with storage.writable_directory(self.observations):
            client.ENVIRONMENT_DESCRIPTION_PATH.unlink()
        code, output = self.cli("environment-description")
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output), {"environment_description": DESCRIPTION})
        self.assertEqual(client.ENVIRONMENT_DESCRIPTION_PATH.read_text(encoding="utf-8"), DESCRIPTION)
        self.assert_readonly()

    def test_real_cli_processes_use_saved_position_and_print_real_paths(self):
        http_server = make_server("127.0.0.1", 0, server.create_app(self.game), request_handler=QuietRequestHandler)
        thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(http_server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(http_server.shutdown)
        storage.write_readonly_file(self.client_dir / storage.CLIENT_CONFIG_FILENAME, json.dumps({
            "server_url": f"http://127.0.0.1:{http_server.server_port}",
            "observations_path": str(self.observations),
        }).encode())

        def run(*args):
            result = subprocess.run(
                [sys.executable, str(self.client_dir / "client.py"), *args], cwd=self.root,
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assert_reported_files_exist(result.stdout)
            return result.stdout

        output = run("start", "alias")
        self.assertEqual(set(self.reported_paths(output)), self.initial_paths("level_01_attempt_01"))
        self.manager.next_observation = observation(state="GAME_OVER")
        run("move", '{"id":"ACTION1"}')
        self.manager.next_observation = observation(label="real-reset")
        output = run("move", '{"id":"RESET"}')
        self.assertEqual(set(self.reported_paths(output)), self.initial_paths("level_01_attempt_02"))
        self.manager.next_observation = observation(completed=1, label="real-transition")
        output = run("move", '{"id":"ACTION1"}')
        self.assertEqual(set(self.reported_paths(output)),
                         self.step_paths("level_01_attempt_02", 1) | self.initial_paths("level_02_attempt_01"))
        output = run("status")
        self.assertEqual(set(self.reported_paths(output)), self.initial_paths("level_02_attempt_01"))
        self.assert_position(3, 2, 1, 0)
        self.assertEqual(inspector.inspect_sessions().step_index, 3)
        self.assert_readonly()


if __name__ == "__main__":
    unittest.main()
