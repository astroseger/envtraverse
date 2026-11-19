"""Launcher configuration tests; no Minecraft download or login required."""

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from . import runtime
from . import install_assets


class RuntimeConfigTests(unittest.TestCase):
    def test_auth_is_explicit_and_uses_same_persistent_database(self):
        with patch.dict(os.environ, {"AUTH_MODE": "offline", "PLAYER_NAME": "TestPlayer"}):
            offline = runtime.launch_command()
        with patch.dict(os.environ, {"AUTH_MODE": "microsoft", "PLAYER_NAME": "TestPlayer"}):
            online = runtime.launch_command()
        self.assertNotIn("--auth", offline)
        self.assertEqual(online, offline + ["--auth"])
        self.assertIn(str(runtime.DATA / "auth" / "portablemc_msa.json"), online)

    def test_invalid_name_and_protocol_are_rejected_before_launch(self):
        for settings in ({"PLAYER_NAME": "-a admin"}, {"MINECRAFT_VERSION": "release"},
                         {"AUTH_MODE": "guess"}, {"WIDTH": "100000"}):
            with self.subTest(settings=settings), patch.dict(os.environ, settings):
                with self.assertRaises(ValueError):
                    runtime.launch_command()

    def test_options_retain_preferences_but_force_realtime_input_behavior(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(runtime, "DATA", Path(directory)):
            options = Path(directory) / "minecraft" / "options.txt"
            options.parent.mkdir()
            options.write_text("mouseSensitivity:0.35\npauseOnLostFocus:true\nrawMouseInput:true\n")
            runtime.configure_options()
            updated = options.read_text()
            self.assertIn("mouseSensitivity:0.35\n", updated)
            self.assertIn("pauseOnLostFocus:false\n", updated)
            self.assertIn("rawMouseInput:false\n", updated)
            self.assertIn("skipMultiplayerWarning:true\n", updated)


class AssetPreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        (self.directory / "logs").mkdir()
        data = patch.object(runtime, "DATA", self.directory)
        data.start()
        self.addCleanup(data.stop)
        environment = patch.dict(os.environ, {"AUTH_MODE": "microsoft", "PLAYER_NAME": "TestPlayer"})
        environment.start()
        self.addCleanup(environment.stop)

    def client(self, install_exit_codes):
        client = runtime.Runtime()
        client.commands = []
        client.statuses = []
        attempts = iter(install_exit_codes)

        def spawn(argv, log_name=None):
            client.commands.append(list(argv))
            if argv[0] == "portablemc":
                code = next(attempts) if "--dry" in argv else 17
            else:
                code = None
            return SimpleNamespace(poll=lambda: code, returncode=code, pid=123)

        def status(state, **fields):
            client.statuses.append((state, fields))
            if state == "exited":
                client.stop()  # The test operator closes the already-exited game.

        client.spawn = Mock(side_effect=spawn)
        client.status = Mock(side_effect=status)
        client.wait_server = Mock()
        return client

    def run_client(self, client):
        with patch.object(runtime, "configure_options"), \
             patch.object(runtime.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="")), \
             patch.object(runtime.time, "sleep"):
            return client.run()

    def test_failed_install_retries_then_launches_game_exactly_once(self):
        client = self.client([1, 0])
        client.wait_install_retry = Mock(return_value=True)
        self.assertEqual(self.run_client(client), 0)
        commands = [command for command in client.commands if command[0] == "portablemc"]
        self.assertEqual(len(commands), 3)
        self.assertTrue(all("--dry" in command and "--auth" not in command for command in commands[:2]))
        self.assertNotIn("--dry", commands[2])
        self.assertIn("--auth", commands[2])
        self.assertIn("--fetch-exclude-all", commands[2])
        self.assertEqual([fields["attempt"] for state, fields in client.statuses if fields.get("phase") == "installing"], [1, 2])
        self.assertEqual(client.wait_install_retry.call_count, 1)
        self.assertIn("attempt 1/3", (self.directory / "logs/launcher.log").read_text())
        self.assertIn("attempt 2/3", (self.directory / "logs/launcher.log").read_text())

    def test_three_failed_installs_never_launch_game(self):
        client = self.client([1, 2, 3])
        client.wait_install_retry = Mock(return_value=True)
        with self.assertRaisesRegex(RuntimeError, "asset preparation failed after 3 attempts"):
            self.run_client(client)
        commands = [command for command in client.commands if command[0] == "portablemc"]
        self.assertEqual(len(commands), 3)
        self.assertTrue(all("--dry" in command for command in commands))
        self.assertEqual([call.args[0] for call in client.wait_install_retry.call_args_list], [1.0, 2.0])

    def test_cancel_during_backoff_prevents_next_attempt_and_game(self):
        client = self.client([1])
        alive = SimpleNamespace(poll=lambda: None)
        with patch.object(runtime.time, "sleep", side_effect=lambda _: client.stop()):
            self.assertFalse(client.prepare_assets(runtime.launch_command(), (alive,)))
        self.assertEqual(client.spawn.call_count, 1)

    def test_cancel_during_install_prevents_game(self):
        client = self.client([None])
        alive = SimpleNamespace(poll=lambda: None)
        with patch.object(runtime.time, "sleep", side_effect=lambda _: client.stop()):
            self.assertFalse(client.prepare_assets(runtime.launch_command(), (alive,)))
        self.assertEqual(client.spawn.call_count, 1)

    def test_display_failure_during_install_is_reported_without_retry(self):
        client = self.client([None])
        dependency = SimpleNamespace(poll=Mock(side_effect=[None, None, 1]))
        with self.assertRaisesRegex(RuntimeError, "display or observation process exited"):
            client.prepare_assets(runtime.launch_command(), (dependency,))
        self.assertEqual(client.spawn.call_count, 1)

    def test_display_failure_during_backoff_is_reported(self):
        client = self.client([])
        dependency = SimpleNamespace(poll=lambda: 1)
        with self.assertRaisesRegex(RuntimeError, "display or observation process exited"):
            client.wait_install_retry(2, (dependency,))
        client.spawn.assert_not_called()

    def test_matching_image_assets_skip_installer_and_launch_once(self):
        root = self.directory / "minecraft"
        for directory in ("assets/indexes", "libraries", "versions/1.21.1"):
            (root / directory).mkdir(parents=True)
        (root / "assets/indexes/17.json").write_text("{}")
        (root / "versions/1.21.1/1.21.1.json").write_text("{}")
        (root / "versions/1.21.1/1.21.1.jar").write_bytes(b"jar")
        (root / install_assets.STAMP_NAME).write_text(json.dumps(install_assets.STAMP))
        client = self.client([])
        self.assertEqual(self.run_client(client), 0)
        commands = [command for command in client.commands if command[0] == "portablemc"]
        self.assertEqual(len(commands), 1)
        self.assertNotIn("--dry", commands[0])
        self.assertIn("--fetch-exclude-all", commands[0])
        self.assertTrue(any(fields.get("source") == "image" for _, fields in client.statuses))

    def test_incomplete_marker_falls_back_to_preparation(self):
        root = self.directory / "minecraft"
        root.mkdir()
        (root / install_assets.STAMP_NAME).write_text(json.dumps(install_assets.STAMP))
        client = self.client([0])
        self.assertEqual(self.run_client(client), 0)
        commands = [command for command in client.commands if command[0] == "portablemc"]
        self.assertEqual(len(commands), 2)
        self.assertIn("--dry", commands[0])

    def test_cancelled_client_does_not_use_cached_assets_or_launch(self):
        client = self.client([])
        client.stop()
        with patch.object(runtime, "cached_assets_ready", return_value=True) as cached:
            self.assertFalse(client.prepare_assets(runtime.launch_command(), ()))
        cached.assert_not_called()
        client.spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
