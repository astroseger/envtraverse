"""Contract compatibility and version selection through the real socket protocol."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from terminal_bench.actions import validate_action
from terminal_bench.catalog import SMOKE_GAME
from terminal_bench.compat import EnvironmentStartError, InvalidActionError
from terminal_bench.description import environment_description
from terminal_bench.environment_manager import EnvironmentManager
from terminal_bench.protocol import RuntimeClient, RuntimeFault, observation
from terminal_bench.runtime import RuntimeService
from tests.fakes import FakeSession


class VersionContractTests(unittest.TestCase):
    def test_default_and_explicit_v1_description_are_unchanged(self):
        expected = (Path(__file__).parent / "fixtures/v1_description.txt").read_text()
        arguments = {"time_budget": {"agent_timeout_seconds": 120}}
        self.assertEqual(environment_description("PUBLIC TASK\n", 32 * 1024 * 1024, **arguments), expected)
        self.assertEqual(environment_description("PUBLIC TASK\n", 32 * 1024 * 1024,
                                                environment_version="v1", **arguments), expected)

    def test_retrieval_actions_are_exclusive_and_descriptions_validate(self):
        for version, retrieval, other in (("v1", "get_file", "get_path"), ("v1.2", "get_path", "get_file")):
            with self.subTest(version=version):
                description = environment_description("PUBLIC TASK", 100, environment_version=version)
                actions = [json.loads(line[2:]) for line in description.splitlines() if line.startswith('- {')]
                self.assertEqual({a["id"] for a in actions}, {
                    "terminal_new", "terminal_send", "terminal_read", "send_path", "finish", retrieval})
                for action in actions:
                    self.assertEqual(validate_action(action, environment_version=version), action)
                rejected = {"id": other, **({"path": "/app/a"} if other == "get_file" else
                                           {"path_sandbox": "/app/a", "path_local": "a"})}
                with self.assertRaises(RuntimeFault):
                    validate_action(rejected, environment_version=version)

    def test_download_paths_are_validated_before_execution(self):
        action = {"id": "get_path", "path_sandbox": "/app/a", "path_local": "a"}
        for name, values in {
            "path_local": ["/tmp/a", "../a", "a/../../b", "", None, "a\0b"],
            "path_sandbox": ["relative", "", None, "a\0b"],
        }.items():
            for value in values:
                with self.subTest(name=name, value=value), self.assertRaises(RuntimeFault):
                    validate_action(action | {name: value}, environment_version="v1.2")

    def test_invalid_versions_fail_without_connecting(self):
        with patch.object(RuntimeClient, "call", side_effect=AssertionError("Unexpected connection")):
            for version in (None, 1.2, "latest", "v1.1", [], {}):
                with self.subTest(version=version), self.assertRaisesRegex(ValueError, "environment_version"):
                    EnvironmentManager({"environment_version": version})
            self.assertEqual(EnvironmentManager().environment_version, "v1")

    def test_new_version_cannot_silently_fall_back_to_an_old_runtime(self):
        manager = EnvironmentManager({"environment_version": "v1.2"})
        old_reply = {"session_id": manager.session_id, "environment_description": "OLD",
                     "observation": observation({"screen.txt": "old"})}
        with patch.object(manager, "_call", return_value=old_reply), patch.object(manager, "stop"):
            with self.assertRaisesRegex(EnvironmentStartError, "confirm.*environment_version"):
                manager.start_environment(SMOKE_GAME)
        manager = EnvironmentManager()
        with patch.object(manager, "_call", return_value=old_reply | {"session_id": manager.session_id}):
            self.assertEqual(manager.start_environment(SMOKE_GAME).environment_description, "OLD")


class VersionSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tb_versions_")
        self.root = Path(self.temporary.name)
        self.service = RuntimeService(socket_path=self.root / "runtime.sock", output_dir=self.root / "results",
                                      runs_dir=self.root / "run", session_factory=FakeSession)
        await self.service.start()

    async def asyncTearDown(self):
        await self.service.stop()
        self.temporary.cleanup()

    async def test_different_versions_share_one_runtime_without_changing_each_other(self):
        managers = [EnvironmentManager({"socket_path": str(self.root / "runtime.sock"),
                                       "environment_version": version}) for version in ("v1", "v1.2")]
        starts = await asyncio.gather(*(asyncio.to_thread(m.start_environment, SMOKE_GAME) for m in managers))
        actions = [{"id": "get_file", "path": "/app/a"},
                   {"id": "get_path", "path_sandbox": "/app/a", "path_local": "a"}]
        for index, manager in enumerate(managers):
            self.assertIn(actions[index]["id"], starts[index].environment_description)
            result = await asyncio.to_thread(manager.move, actions[index])
            self.assertIn("file.bin" if index == 0 else "transfer.json", result.files)
            with self.assertRaises(InvalidActionError):
                await asyncio.to_thread(manager.move, actions[1 - index])
            # The runtime independently checks the session's bound version.
            with self.assertRaises(RuntimeFault):
                await asyncio.to_thread(manager._call, "action", action=actions[1 - index],
                                        environment_version=managers[1 - index].environment_version)
            session = self.service.sessions[manager.session_id]
            self.assertEqual(session.calls, [actions[index]])
            manifest = json.loads((self.root / "results" / manager.session_id / "session.json").read_text())
            self.assertEqual(manifest["environment_version"], manager.environment_version)
            await asyncio.to_thread(manager.move, {"id": "finish"})

    async def test_runtime_rejects_unknown_version_before_reserving_session(self):
        client = RuntimeClient(self.root / "runtime.sock")
        session = uuid4().hex
        with self.assertRaisesRegex(RuntimeFault, "environment_version"):
            await asyncio.to_thread(client.call, "start", session_id=session, owner_token=uuid4().hex,
                                    game_id=SMOKE_GAME, environment_version="v9")
        self.assertFalse((self.root / "results" / session).exists())
        self.assertFalse(self.service.sessions)
