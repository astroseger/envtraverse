"""Real Unix socket + new single-game manager; backend is explicitly a test double."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import patch

from terminal_bench.catalog import BenchmarkCatalog, SMOKE_GAME
from terminal_bench.compat import EnvironmentError, EnvironmentStartError, InvalidActionError
from terminal_bench.environment_manager import EnvironmentManager
from terminal_bench.protocol import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, PROTOCOL_VERSION, RuntimeClient, RuntimeFault, loads, pack, _recv_exact
from terminal_bench.runtime import RuntimeService
from tests.fakes import FakeSession


class SocketIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tb_")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        task = self.repo / "tasks" / "fix-git"
        task.mkdir(parents=True)
        (task / "task.toml").write_text('version = "1.0"\n')
        (task / "instruction.md").write_text("PUBLIC INSTRUCTION ONLY\n")
        (task / "secret.txt").write_text("PRIVATE TEST SECRET")
        self.catalog = BenchmarkCatalog(self.repo, smoke_task=task)
        self.path = self.root / "runtime.sock"
        self.service = RuntimeService(socket_path=self.path, catalog=self.catalog,
                                      output_dir=self.root / "out", runs_dir=self.root / "run", session_factory=FakeSession)
        await self.service.start()

    async def asyncTearDown(self):
        await self.service.stop()
        self.temp.cleanup()

    def manager(self, **kwargs):
        return EnvironmentManager({"socket_path": str(self.path), **kwargs})

    async def test_complete_pair_new_api(self):
        def scenario():
            manager = self.manager()
            start = manager.start_environment(SMOKE_GAME)
            self.assertFalse(hasattr(start, "game"))
            self.assertIn("PUBLIC INSTRUCTION ONLY", start.environment_description)
            self.assertNotIn("PRIVATE TEST SECRET", start.environment_description)
            self.assertEqual(json.loads(start.observation.files["session.json"])["session_id"], manager.session_id)
            manager.move({"id": "terminal_send", "terminal_id": "t1", "keys": [{"text": "pwd"}, {"key": "Enter"}]})
            self.assertIn("screen.txt", manager.move({"id": "terminal_read", "terminal_id": "t1"}).files)
            second = manager.move({"id": "terminal_new"})
            self.assertEqual(json.loads(second.files["terminal.json"])["terminal_id"], "t2")
            self.assertEqual(manager.move({"id": "get_file", "path": "/app/a"}).files["file.bin"], b"\0\xffbinary\n")
            self.assertTrue(manager.move({"id": "finish"}).terminal)
            with self.assertRaises(InvalidActionError):
                manager.move({"id": "terminal_new"})
            manager.stop()
            manager.stop()
            self.assertTrue(manager.client.call("health")["ready"])
        self.assertFalse(self.service.sessions)
        await asyncio.to_thread(scenario)
        session = next(iter(self.service.sessions.values()))
        self.assertEqual(session.close_count, 1)
        self.assertEqual(session.start_count, 1)

    async def test_constructor_and_stop_before_start_do_not_connect(self):
        with patch.object(RuntimeClient, "call", side_effect=AssertionError("Unexpected network I/O")):
            manager = self.manager()
            manager.stop()
            manager.stop()
            with self.assertRaises(EnvironmentStartError):
                manager.start_environment(SMOKE_GAME)

    async def test_default_and_null_timeout_allow_delayed_start(self):
        start = FakeSession.start

        async def delayed_start(session):
            await asyncio.sleep(0.1)
            return await start(session)

        with patch.object(FakeSession, "start", delayed_start):
            for parameters in ({}, {"request_timeout_seconds": None}):
                with self.subTest(parameters=parameters):
                    manager = self.manager(**parameters)
                    self.assertIsNone(manager.client.timeout)
                    try:
                        result = await asyncio.wait_for(
                            asyncio.to_thread(manager.start_environment, SMOKE_GAME), 2)
                        self.assertIn("PUBLIC INSTRUCTION ONLY", result.environment_description)
                    finally:
                        await asyncio.to_thread(manager.stop)

    async def test_numeric_timeout_still_limits_socket_wait(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        dispatch = self.service.dispatch

        async def delayed_dispatch(request):
            entered.set()
            await release.wait()
            return await dispatch(request)

        manager = self.manager(request_timeout_seconds=0.05)
        with patch.object(self.service, "dispatch", delayed_dispatch):
            pending = asyncio.create_task(asyncio.to_thread(manager.client.call, "health"))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                done, _ = await asyncio.wait({pending}, timeout=2)
                self.assertIn(pending, done, "Configured socket timeout did not expire")
                with self.assertRaises(TimeoutError):
                    await pending
            finally:
                release.set()
                await asyncio.gather(pending, return_exceptions=True)

    async def test_no_timeout_still_reports_connection_closure(self):
        async def close_connection(reader, writer):
            writer.close()
            await writer.wait_closed()

        path = self.root / "closed.sock"
        server = await asyncio.start_unix_server(close_connection, path=path)
        try:
            manager = EnvironmentManager({"socket_path": str(path)})
            with self.assertRaises(ConnectionError):
                await asyncio.wait_for(asyncio.to_thread(manager.client.call, "health"), 2)
        finally:
            server.close()
            await server.wait_closed()

    async def test_exactly_one_start_even_when_first_fails(self):
        def scenario():
            manager = self.manager()
            with self.assertRaises(EnvironmentStartError):
                manager.start_environment("unknown-task")
            with self.assertRaises(EnvironmentStartError):
                manager.start_environment(SMOKE_GAME)
            manager.stop()
            return manager.session_id
        key = await asyncio.to_thread(scenario)
        self.assertFalse(self.service.sessions)
        data = json.loads((self.root / "out" / key / "result.json").read_text())
        self.assertEqual(data["status"], "error")

    async def test_two_managers_have_independent_sessions(self):
        def scenario():
            a, b = self.manager(), self.manager()
            a.start_environment(SMOKE_GAME)
            b.start_environment("terminal-bench-2.1-fix-git")
            a.stop()
            self.assertFalse(b.move({"id": "terminal_read", "terminal_id": "t1"}).terminal)
            b.stop()
            return a.session_id, b.session_id
        a, b = await asyncio.to_thread(scenario)
        self.assertNotEqual(a, b)
        self.assertEqual(len(self.service.sessions), 2)

    async def test_duplicate_cannot_close_original_session(self):
        def scenario():
            key = uuid4().hex
            a, b = self.manager(session_id=key), self.manager(session_id=key)
            a.start_environment(SMOKE_GAME)
            with self.assertRaises(EnvironmentStartError):
                b.start_environment(SMOKE_GAME)
            b.stop()
            self.assertFalse(self.service.sessions[key].closed)
            self.assertFalse(a.move({"id": "terminal_new"}).terminal)
            a.stop()
        await asyncio.to_thread(scenario)

    async def test_wrong_owner_cannot_act_or_close(self):
        def scenario():
            a = self.manager()
            a.start_environment(SMOKE_GAME)
            for method, kwargs in [("close", {}), ("action", {"action": {"id": "terminal_new"}})]:
                with self.assertRaisesRegex(RuntimeFault, "does not own"):
                    a.client.call(method, session_id=a.session_id, owner_token=uuid4().hex, **kwargs)
            a.stop()
        await asyncio.to_thread(scenario)

    async def test_duplicate_after_restart_or_existing_result_refused(self):
        key = uuid4().hex
        directory = self.root / "out" / key
        directory.mkdir()
        (directory / "result.json").write_text('{"original": true}')
        def scenario():
            manager = self.manager(session_id=key)
            with self.assertRaises(EnvironmentStartError):
                manager.start_environment(SMOKE_GAME)
        await asyncio.to_thread(scenario)
        self.assertEqual(json.loads((directory / "result.json").read_text()), {"original": True})

    async def test_invalid_action_not_dispatched(self):
        def scenario():
            manager = self.manager()
            manager.start_environment(SMOKE_GAME)
            with self.assertRaises(InvalidActionError):
                manager.move({"id": "terminal_send", "terminal_id": "t1", "keys": [{"key": "INVALID"}]})
            manager.stop()
        await asyncio.to_thread(scenario)
        self.assertFalse(next(iter(self.service.sessions.values())).calls)

    async def test_oversized_actions_leave_session_usable(self):
        def scenario():
            manager = self.manager()
            manager.start_environment(SMOKE_GAME)
            session = self.service.sessions[manager.session_id]
            valid = {"id": "terminal_read", "terminal_id": "t1"}
            cases = [
                ({"id": "terminal_send", "terminal_id": "t1",
                  "keys": [{"text": "x" * MAX_REQUEST_BYTES}]}, "65536"),
                ({"id": "terminal_read", "terminal_id": "t" + "1" * MAX_REQUEST_BYTES},
                 str(MAX_REQUEST_BYTES)),
            ]
            try:
                for action, message in cases:
                    with self.subTest(action=action["id"]):
                        calls = len(session.calls)
                        with patch("terminal_bench.protocol.socket.socket") as socket_factory:
                            with self.assertRaisesRegex(InvalidActionError, message):
                                manager.move(action)
                            socket_factory.assert_not_called()
                        self.assertEqual(len(session.calls), calls)
                        self.assertFalse(session.closed)
                        self.assertFalse(manager.move(valid).terminal)
                self.assertEqual(session.calls, [valid, valid])
            finally:
                manager.stop()
        await asyncio.to_thread(scenario)

    async def test_uuid_and_configuration_validation(self):
        key = uuid4()
        self.assertEqual(self.manager(session_id=str(key)).session_id, key.hex)
        for value in ["../a", "anything", None, 1]:
            with self.assertRaises(ValueError):
                self.manager(session_id=value)
        for timeout in [0, -1, True, False, "3600", [], float("nan"), float("inf"), -float("inf")]:
            with self.assertRaises(ValueError):
                self.manager(request_timeout_seconds=timeout)

    async def test_unknown_session_idempotent_close(self):
        def scenario():
            client = RuntimeClient(self.path)
            identity = {"session_id": uuid4().hex, "owner_token": uuid4().hex}
            self.assertTrue(client.call("close", **identity)["closed"])
            with self.assertRaises(RuntimeFault):
                client.call("action", action={"id": "terminal_new"}, **identity)
        await asyncio.to_thread(scenario)

    async def test_relative_socket_bind_works_in_a_long_checkout_path(self):
        # Socket path length is checked on the spelling passed to bind(), not
        # on the physical absolute location of its parent directory.
        directory = self.root / ("long-checkout-" * 12)
        (directory / "socket").mkdir(parents=True)
        previous = Path.cwd()
        service = RuntimeService(socket_path=Path("socket/runtime.sock"), output_dir=directory / "results", runs_dir=directory / "run")
        try:
            os.chdir(directory)
            await service.start()
            self.assertGreater(len(str(directory / "socket/runtime.sock")), 107)
            health = await asyncio.to_thread(RuntimeClient("socket/runtime.sock").call, "health")
            self.assertTrue(health["ready"])
        finally:
            await service.stop()
            os.chdir(previous)
        self.assertFalse((directory / "socket/runtime.sock").exists())

    async def test_completion_summary_reports_idle_and_stays_ready(self):
        manager = self.manager()
        await asyncio.to_thread(manager.start_environment, SMOKE_GAME)
        with self.assertLogs("terminal_bench.runtime", level="INFO") as logs:
            await asyncio.to_thread(manager.stop)
            await self.service.completions[manager.session_id]
        self.assertEqual(self.service.outcomes[manager.session_id], "validated")
        self.assertIn("No active sessions.", "\n".join(logs.output))
        self.assertTrue((await asyncio.to_thread(manager.client.call, "health"))["ready"])

    async def test_socket_permissions_collision(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        another = RuntimeService(socket_path=self.path, output_dir=self.root / "other", runs_dir=self.root / "run")
        with self.assertRaises(RuntimeError):
            await another.start()

    async def test_malformed_requests(self):
        def raw(data):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.connect(str(self.path))
                connection.sendall(data)
                size = struct.unpack("!I", _recv_exact(connection, 4))[0]
                return loads(_recv_exact(connection, size))
        for data in [struct.pack("!I", MAX_REQUEST_BYTES + 1), struct.pack("!I", 2) + b"[]",
                     pack({"version": -1, "method": "health"}, MAX_REQUEST_BYTES)]:
            result = await asyncio.to_thread(raw, data)
            self.assertFalse(result["ok"])
        self.assertTrue((await asyncio.to_thread(RuntimeClient(self.path).call, "health"))["ready"])

    async def test_task_prefixes_and_symlink_escape(self):
        for game in ["smoke-task", "terminal-bench-4.0-fix-git", "terminal-bench-2.1-../fix-git", "smoke-test-other"]:
            with self.assertRaises(RuntimeFault):
                self.catalog.resolve(game)
        other = self.root / "external"
        other.mkdir()
        (other / "task.toml").write_text("")
        (other / "instruction.md").write_text("")
        (self.repo / "tasks" / "escape").symlink_to(other, target_is_directory=True)
        with self.assertRaises(RuntimeFault):
            self.catalog.resolve("terminal-bench-2.1-escape")
