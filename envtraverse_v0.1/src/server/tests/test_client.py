from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
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
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import requests
from werkzeug.serving import make_server

SRC_DIR = Path(__file__).resolve().parents[2]
AGENT_SRC = SRC_DIR / "agents" / "agent_v1" / "agent"
sys.path.insert(0, str(SRC_DIR / "server"))
sys.path.insert(0, str(AGENT_SRC / "client"))
sys.path.insert(0, str(AGENT_SRC))

import agent
import client
from client_storage import CLIENT_CONFIG_FILENAME, initialize_client_storage, writable_directory
import server
from environment_api import Observation
from environments.example import environment_manager as example


class MixedOutputManager(example.EnvironmentManager):
    def observation(self) -> Observation:
        observation = super().observation()
        return Observation(
            files={
                **observation.files,
                "unicode.txt": "Text: 日本語, ქართული.\n",
                "data.bin": bytes(range(256)),
            },
            terminal=observation.terminal,
        )


class ClientIntegrationTest(unittest.TestCase):
    """Use the single-game server routes, with HTTP requests bridged to Flask."""

    def setUp(self) -> None:
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.workspace = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        client_dir = self.workspace / "client"
        shutil.copytree(AGENT_SRC / "client", client_dir, ignore=shutil.ignore_patterns("__pycache__"))
        observations = self.workspace / "observations"
        observations.mkdir()
        initialize_client_storage(client_dir, observations, server_url="http://env-server:8879")
        stack.enter_context(patch.multiple(
            client,
            CLIENT_DIR=client_dir,
            OBSERVATIONS_DIR=observations,
            ACTIONS_DIR=observations / "actions",
            CLIENT_STATE_PATH=client_dir / ".client_session.json",
            CLIENT_LOCK_PATH=client_dir / ".client.lock",
            ENVIRONMENT_DESCRIPTION_PATH=observations / "environment_description.md",
        ))
        stack.enter_context(patch.object(agent, "ENVIRONMENT_DESCRIPTION_PATH", client.ENVIRONMENT_DESCRIPTION_PATH))
        manager = MixedOutputManager({"target": 3})
        self.game_server = server.GameServer(manager, game_id="counter", game_alias="game1")
        self.http = server.create_app(self.game_server).test_client()
        self.post = stack.enter_context(patch.object(client.HTTP_SESSION, "post", side_effect=self.http_post))
        self.get = stack.enter_context(patch.object(client.HTTP_SESSION, "get", side_effect=self.http_get))
        self.output = io.StringIO()
        stack.enter_context(redirect_stdout(self.output))
        self.addCleanup(self.game_server.stop)

    @staticmethod
    def requests_response(response) -> requests.Response:
        wrapped = requests.Response()
        wrapped.status_code = response.status_code
        wrapped._content = response.data
        return wrapped

    def http_post(self, url, *, json, headers, timeout):
        self.assertEqual(timeout, (10.0, 3900.0))
        self.assert_storage_readonly()
        return self.requests_response(self.http.post(urlsplit(url).path, json=json, headers=headers))

    def http_get(self, url, *, headers, timeout):
        self.assertEqual(timeout, (10.0, 3900.0))
        self.assert_storage_readonly()
        return self.requests_response(self.http.get(urlsplit(url).path, headers=headers))

    def assert_storage_readonly(self) -> None:
        for directory in (client.CLIENT_DIR, client.OBSERVATIONS_DIR):
            for path in (directory, *directory.rglob("*")):
                expected = 0o555 if path.is_dir() else 0o444
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected, str(path))

    def test_start_saves_authoritative_description_text_and_binary(self) -> None:
        (self.workspace / "environment_description.md").write_text("Old workspace description", encoding="utf-8")
        client.start_command("game1")
        self.assertIn("Reach 3", client.ENVIRONMENT_DESCRIPTION_PATH.read_text(encoding="utf-8"))
        self.assertEqual((client.OBSERVATIONS_DIR / "step_0000_data.bin").read_bytes(), bytes(range(256)))
        self.assertEqual(
            (client.OBSERVATIONS_DIR / "step_0000_unicode.txt").read_text(encoding="utf-8"),
            "Text: 日本語, ქართული.\n",
        )
        state = client.read_client_state()
        self.assertEqual(state["step_index"], 0)
        self.assertFalse(state["terminal"])
        self.assertEqual(set(state), {"observations_path", "token", "step_index", "terminal"})
        self.assertNotIn("server_url", json.loads(client.CLIENT_STATE_PATH.read_text(encoding="utf-8")))
        fake_agent = SimpleNamespace(run_dir=self.workspace, logger=Mock(), run_client=Mock())
        self.assertIsNone(agent.SimpleAgent.validate_environment_description(fake_agent))
        fake_agent.run_client.assert_not_called()
        self.assert_storage_readonly()
        self.assertIn(f"path: {client.OBSERVATIONS_DIR}", self.output.getvalue())
        self.assertIn(f"environment description: {client.ENVIRONMENT_DESCRIPTION_PATH}", self.output.getvalue())

    def test_move_saves_actions_and_refuses_to_advance_after_terminal(self) -> None:
        client.start_command("game1")
        for _ in range(3):
            client.move_command('{"id":"INC"}')
        state = client.read_client_state()
        self.assertEqual(state["step_index"], 3)
        self.assertTrue(state["terminal"])
        self.assertEqual(
            json.loads((client.ACTIONS_DIR / "action_0002.txt").read_text(encoding="utf-8")),
            {"id": "INC"},
        )
        post_count = self.post.call_count
        with self.assertRaisesRegex(RuntimeError, "terminal"):
            client.move_command('{"id":"INC"}')
        self.assertEqual(self.post.call_count, post_count)
        self.assert_storage_readonly()

    def test_status_recovers_uncommitted_step_without_replaying_action(self) -> None:
        client.start_command("game1")
        state = client.read_client_state()
        response = self.http.post(
            "/game/action",
            headers={"X-Session-Token": state["token"]},
            json={"action": {"id": "INC"}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.read_client_state()["step_index"], 0)
        client.status_command()
        self.assertEqual(client.read_client_state()["step_index"], 1)
        self.assertEqual(
            (client.OBSERVATIONS_DIR / "step_0001_state.txt").read_text(encoding="utf-8"),
            "value: 1\ntarget: 3\n",
        )
        self.assertEqual((client.OBSERVATIONS_DIR / "step_0001_data.bin").read_bytes(), bytes(range(256)))
        self.assertTrue((client.ACTIONS_DIR / "action_0000.txt").is_file())
        self.assertEqual(self.post.call_count, 1)  # Only start passed through the client's POST.
        client.move_command('{"id":"INC"}')
        self.assertEqual(client.read_client_state()["step_index"], 2)

    def test_description_command_restores_cached_description(self) -> None:
        client.start_command("game1")
        expected = client.ENVIRONMENT_DESCRIPTION_PATH.read_text(encoding="utf-8")
        with writable_directory(client.OBSERVATIONS_DIR):
            client.ENVIRONMENT_DESCRIPTION_PATH.unlink()
        self.output.seek(0)
        self.output.truncate()
        client.environment_description_command()
        self.assertEqual(json.loads(self.output.getvalue()), {"environment_description": expected})
        self.assertEqual(client.ENVIRONMENT_DESCRIPTION_PATH.read_text(encoding="utf-8"), expected)
        self.assert_storage_readonly()

    def test_description_is_required_and_agent_rejects_a_missing_file(self) -> None:
        for payload in ({}, {"environment_description": None}, {"environment_description": ""}):
            with self.subTest(payload=payload), self.assertRaisesRegex(RuntimeError, "description"):
                client.save_environment_description(payload)
        self.assertFalse(client.ENVIRONMENT_DESCRIPTION_PATH.exists())
        fake_agent = SimpleNamespace(run_dir=self.workspace, logger=Mock())
        with self.assertRaisesRegex(RuntimeError, "description"):
            agent.SimpleAgent.validate_environment_description(fake_agent)

    @unittest.skipIf(os.geteuid() == 0, "root bypasses ordinary POSIX permission checks")
    def test_normal_edits_creates_and_deletes_are_denied(self) -> None:
        client.start_command("game1")
        client.move_command('{"id":"INC"}')
        for directory in (client.CLIENT_DIR, client.OBSERVATIONS_DIR, client.ACTIONS_DIR):
            with self.subTest(directory=directory), self.assertRaises(PermissionError):
                (directory / "accidental.txt").write_text("oops", encoding="utf-8")
        for path in (
            client.CLIENT_DIR / "client.py", client.CLIENT_STATE_PATH,
            client.CLIENT_DIR / CLIENT_CONFIG_FILENAME,
            client.ENVIRONMENT_DESCRIPTION_PATH, client.OBSERVATIONS_DIR / "step_0000_data.bin",
            client.ACTIONS_DIR / "action_0000.txt",
        ):
            with self.subTest(path=path):
                with self.assertRaises(PermissionError):
                    path.write_bytes(b"oops")
                with self.assertRaises(PermissionError):
                    path.unlink()
        self.assert_storage_readonly()

    def test_start_rejects_old_observations_before_contacting_server(self) -> None:
        with writable_directory(client.OBSERVATIONS_DIR):
            old_path = client.OBSERVATIONS_DIR / "old.txt"
            old_path.write_text("old", encoding="utf-8")
            old_path.chmod(0o444)
        with self.assertRaisesRegex(FileExistsError, "empty"):
            client.start_command("game1")
        self.post.assert_not_called()
        self.assert_storage_readonly()

    def test_start_rejects_existing_session_before_contacting_server(self) -> None:
        client.start_command("game1")
        with self.assertRaisesRegex(FileExistsError, "session"):
            client.start_command("game1")
        self.assertEqual(self.post.call_count, 1)
        self.assert_storage_readonly()

    def test_all_commands_ignore_client_environment_overrides(self) -> None:
        with patch.dict(os.environ, {
            "ENV_SERVER_URL": "http://wrong-server:1",
            "ENV_OBSERVATIONS_DIR": str(self.workspace / "wrong-observations"),
            "ENV_CLIENT_DIR": str(self.workspace / "wrong-client"),
        }):
            client.start_command("game1")
            client.move_command('{"id":"INC"}')
            client.status_command()
            client.environment_description_command()
        for call in self.post.call_args_list + self.get.call_args_list:
            self.assertTrue(call.args[0].startswith("http://env-server:8879/"), call.args[0])
        self.assertFalse((self.workspace / "wrong-observations").exists())
        self.assertFalse(client.HTTP_SESSION.trust_env)
        self.assert_storage_readonly()

    def test_missing_config_never_falls_back_to_environment_or_saved_session(self) -> None:
        client.start_command("game1")
        with writable_directory(client.CLIENT_DIR):
            (client.CLIENT_DIR / CLIENT_CONFIG_FILENAME).unlink()
        self.post.reset_mock()
        self.get.reset_mock()
        with patch.dict(os.environ, {"ENV_SERVER_URL": "http://wrong-server:1"}):
            for command, args in (
                (client.start_command, ("game1",)), (client.move_command, ('{"id":"INC"}',)),
                (client.status_command, ()), (client.environment_description_command, ()),
            ):
                with self.subTest(command=command.__name__), self.assertRaisesRegex(RuntimeError, "configuration is missing"):
                    command(*args)
        self.post.assert_not_called()
        self.get.assert_not_called()

    def test_active_session_and_recovery_use_the_url_from_configuration(self) -> None:
        client.start_command("game1")
        state = client.read_client_state()
        response = self.http.post(
            "/game/action",
            headers={"X-Session-Token": state["token"]},
            json={"action": {"id": "INC"}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.read_client_state()["step_index"], 0)
        configuration = {
            "server_url": "http://different-server:9988",
            "observations_path": str(client.OBSERVATIONS_DIR),
        }
        with client.client_lock():
            client.write_readonly_file(
                client.CLIENT_DIR / CLIENT_CONFIG_FILENAME,
                json.dumps(configuration).encode("utf-8"),
            )
        self.post.reset_mock()
        self.get.reset_mock()
        client.status_command()  # Recover the uncommitted step using the configured URL.
        self.assertEqual(client.read_client_state()["step_index"], 1)
        self.post.assert_not_called()
        client.move_command('{"id":"INC"}')
        client.status_command()
        client.environment_description_command()
        self.assertEqual(client.read_client_state()["step_index"], 2)
        self.assertNotIn("server_url", json.loads(client.CLIENT_STATE_PATH.read_text(encoding="utf-8")))
        for call in self.post.call_args_list + self.get.call_args_list:
            self.assertTrue(call.args[0].startswith("http://different-server:9988/"), call.args[0])
        self.assert_storage_readonly()

    def test_session_writer_rejects_a_server_url_and_preserves_state(self) -> None:
        client.start_command("game1")
        state = client.read_client_state()
        state_before = client.CLIENT_STATE_PATH.read_bytes()
        with client.client_lock(), self.assertRaisesRegex(RuntimeError, "unexpected keys"):
            client.write_client_state({**state, "server_url": "http://wrong-server:1"})
        self.assertEqual(client.CLIENT_STATE_PATH.read_bytes(), state_before)
        self.assert_storage_readonly()

    def test_failed_session_commit_is_atomic_and_recovery_replaces_readonly_artifacts(self) -> None:
        client.start_command("game1")
        previous_state = client.CLIENT_STATE_PATH.read_bytes()
        real_replace = Path.replace

        def fail_session_replace(source, target):
            if target == client.CLIENT_STATE_PATH:
                raise OSError("simulated failed session commit")
            return real_replace(source, target)

        with patch.object(Path, "replace", fail_session_replace):
            with self.assertRaisesRegex(OSError, "session commit"):
                client.move_command('{"id":"INC"}')
        self.assertEqual(client.CLIENT_STATE_PATH.read_bytes(), previous_state)
        self.assertTrue((client.OBSERVATIONS_DIR / "step_0001_data.bin").is_file())
        self.assertFalse(list(self.workspace.rglob(".client-write-*")))
        self.assert_storage_readonly()
        client.status_command()
        self.assertEqual(client.read_client_state()["step_index"], 1)
        self.assertEqual(self.post.call_count, 2)  # Recovery did not replay the action.
        self.assert_storage_readonly()

    def test_failed_artifact_save_reseals_storage_and_status_recovers(self) -> None:
        client.start_command("game1")
        with patch.object(Path, "replace", side_effect=OSError("simulated disk error")):
            with self.assertRaisesRegex(OSError, "disk error"):
                client.move_command('{"id":"INC"}')
        self.assertEqual(client.read_client_state()["step_index"], 0)
        self.assertFalse(list(self.workspace.rglob(".client-write-*")))
        self.assert_storage_readonly()
        client.status_command()
        self.assertEqual(client.read_client_state()["step_index"], 1)
        self.assertEqual(self.post.call_count, 2)
        self.assert_storage_readonly()

    def test_next_command_reseals_directories_left_writable_by_a_killed_client(self) -> None:
        client.start_command("game1")
        client.move_command('{"id":"INC"}')
        for directory in (client.CLIENT_DIR, client.OBSERVATIONS_DIR, client.ACTIONS_DIR):
            directory.chmod(0o755)
        client.status_command()  # HTTP bridge also checks permissions before the request.
        self.assert_storage_readonly()

    def test_lock_serializes_writes_without_replacing_its_readonly_inode(self) -> None:
        inode = client.CLIENT_LOCK_PATH.stat().st_ino
        ready = threading.Event()
        acquired = threading.Event()
        errors = []

        def acquire_in_thread():
            try:
                ready.set()
                with client.client_lock():
                    acquired.set()
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=acquire_in_thread, daemon=True)
        try:
            with client.client_lock(), writable_directory(client.OBSERVATIONS_DIR):
                thread.start()
                self.assertTrue(ready.wait(2))
                self.assertFalse(acquired.wait(0.1))
                self.assertEqual(stat.S_IMODE(client.OBSERVATIONS_DIR.stat().st_mode), 0o755)
        finally:
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(acquired.is_set())
        self.assertEqual(client.CLIENT_LOCK_PATH.stat().st_ino, inode)
        self.assert_storage_readonly()


class ClientSubprocessTest(unittest.TestCase):
    def test_copied_client_ignores_environment_proxies_and_working_directory_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client_dir = root / "client"
            observations = root / "observations"
            workspace = root / "run"
            shutil.copytree(AGENT_SRC / "client", client_dir, ignore=shutil.ignore_patterns("__pycache__"))
            observations.mkdir()
            workspace.mkdir()
            shadow_configuration = workspace / CLIENT_CONFIG_FILENAME
            shadow_configuration.write_text(json.dumps({
                "server_url": "http://127.0.0.1:1",
                "observations_path": str(root / "wrong-observations"),
            }), encoding="utf-8")
            manager = example.EnvironmentManager({"target": 2})
            game_server = server.GameServer(manager, game_id="counter", game_alias="public-alias")
            http = make_server("127.0.0.1", 0, server.create_app(game_server))
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            try:
                configured_url = f"http://127.0.0.1:{http.server_port}"
                initialize_client_storage(client_dir, observations, server_url=configured_url)
                config_before = (client_dir / CLIENT_CONFIG_FILENAME).read_bytes()
                process_env = dict(
                    os.environ,
                    ENV_SERVER_URL="http://127.0.0.1:1",
                    ENV_CLIENT_DIR=str(root / "wrong-client"),
                    ENV_OBSERVATIONS_DIR=str(root / "wrong-observations"),
                    HTTP_PROXY="http://127.0.0.1:1", http_proxy="http://127.0.0.1:1",
                    HTTPS_PROXY="http://127.0.0.1:1", https_proxy="http://127.0.0.1:1",
                    ALL_PROXY="http://127.0.0.1:1", all_proxy="http://127.0.0.1:1",
                    NO_PROXY="", no_proxy="",
                    PYTHONDONTWRITEBYTECODE="1",
                )
                for index, arguments in enumerate((
                    ["start", "public-alias"], ["move", '{"id":"INC"}'],
                    ["status"], ["environment-description"], ["move", '{"id":"INC"}'],
                )):
                    command_env = process_env if index % 2 == 0 else {"PYTHONDONTWRITEBYTECODE": "1"}
                    result = subprocess.run(
                        [sys.executable, str(client_dir / "client.py"), *arguments],
                        cwd=workspace, env=command_env, capture_output=True, text=True, timeout=10,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    if arguments[0] != "environment-description":
                        self.assertIn(f"path: {observations}", result.stdout)
                    for storage_dir in (client_dir, observations):
                        for path in (storage_dir, *storage_dir.rglob("*")):
                            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o555 if path.is_dir() else 0o444)
                state = json.loads((client_dir / ".client_session.json").read_text(encoding="utf-8"))
                self.assertEqual(state["step_index"], 2)
                self.assertTrue(state["terminal"])
                self.assertEqual(set(state), {"observations_path", "token", "step_index", "terminal"})
                self.assertEqual(state["observations_path"], str(observations))
                self.assertEqual((client_dir / CLIENT_CONFIG_FILENAME).read_bytes(), config_before)
                self.assertEqual(list(workspace.iterdir()), [shadow_configuration])
                self.assertFalse((root / "wrong-observations").exists())
                self.assertFalse((root / "wrong-client").exists())
            finally:
                http.shutdown()
                thread.join(timeout=2)
                http.server_close()
                game_server.stop()

    def test_cli_reports_missing_and_invalid_config_without_tracebacks_or_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client_dir = root / "client"
            shutil.copytree(AGENT_SRC / "client", client_dir, ignore=shutil.ignore_patterns("__pycache__"))
            config_path = client_dir / CLIENT_CONFIG_FILENAME
            process_env = dict(os.environ, ENV_SERVER_URL="http://127.0.0.1:1", PYTHONDONTWRITEBYTECODE="1")
            for content in (None, b"{", b"\xff", b"[]", b"{}", b'{"server_url":"","observations_path":"/observations"}'):
                with self.subTest(content=content):
                    config_path.unlink(missing_ok=True)
                    if content is not None:
                        config_path.write_bytes(content)
                    result = subprocess.run(
                        [sys.executable, str(client_dir / "client.py"), "start", "public-alias"],
                        cwd=root, env=process_env, capture_output=True, text=True, timeout=10,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("configuration", result.stdout)
                    self.assertIn(str(config_path), result.stdout)
                    self.assertNotIn("Traceback", result.stdout + result.stderr)
                    self.assertNotIn("Server request failed", result.stdout)
                    self.assertFalse((client_dir / ".client_session.json").exists())
                    self.assertFalse((client_dir / ".client.lock").exists())
            config_path.unlink()
            help_result = subprocess.run(
                [sys.executable, str(client_dir / "client.py"), "--help"],
                cwd=root, env=process_env, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(help_result.returncode, 0, help_result.stderr)


if __name__ == "__main__":
    unittest.main()
