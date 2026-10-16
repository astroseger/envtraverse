from __future__ import annotations

import hashlib
import json
import unittest

from terminal_bench.protocol import MAX_RESPONSE_BYTES, RuntimeFault, decode_observation, loads, pack
from terminal_bench.settings import TerminalSettings
from terminal_bench.terminal import TmuxTerminals
from tests.fakes import ScriptedEnvironment


class TerminalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = ScriptedEnvironment()
        self.term = TmuxTerminals(self.env, TerminalSettings(install_tmux=False))
        await self.term.setup()
        await self.term.new()

    async def test_actual_geometry_reported(self):
        raw = await self.term.read("t1")
        files, ended = decode_observation(raw)
        self.assertFalse(ended)
        meta = json.loads(files["terminal.json"])
        self.assertEqual((meta["columns"], meta["rows"]), (160, 40))
        self.assertTrue(meta["stdout_stderr_combined"])

    async def test_literal_and_special_are_different(self):
        text = "C-c; '\"Ω\n $(do_not_execute_in_host)"
        await self.term.perform({"id": "terminal_send", "terminal_id": "t1",
                                 "keys": [{"text": text}, {"key": "C-c"}]})
        pane = self.env.panes["%0"]
        self.assertEqual(bytes(pane["input"]), text.encode())
        self.assertEqual(pane["keys"], ["C-c"])
        # Text never appears as raw tmux command-language input.
        sends = [x["command"] for x in self.env.calls if "send-keys" in x["command"]]
        self.assertTrue(any("-H" in cmd for cmd in sends))
        self.assertFalse(any("do_not_execute" in cmd for cmd in sends))

    async def test_prevalidate_entire_send_before_mutation(self):
        before = len(self.env.calls)
        with self.assertRaises(RuntimeFault):
            await self.term.perform({"id": "terminal_send", "terminal_id": "t1", "keys": [
                {"text": "must not be sent"}, {"key": "INVALID"}]})
        self.assertEqual(len(self.env.calls), before)

    async def test_independent_terminal_handles_beyond_former_limits(self):
        for number in range(2, 41):
            files, _ = decode_observation(await self.term.perform({"id": "terminal_new"}))
            self.assertEqual(json.loads(files["terminal.json"])["terminal_id"], f"t{number}")
        await self.term.send("t1", [{"text": "one"}])
        await self.term.send("t40", [{"text": "forty"}])
        self.assertEqual(bytes(self.env.panes["%0"]["input"]), b"one")
        self.assertEqual(bytes(self.env.panes["%39"]["input"]), b"forty")

    async def test_history_is_bounded_and_marked(self):
        self.env.panes["%0"]["screen"] = "one\ntwo\nthree\n"
        files, _ = decode_observation(await self.term.read("t1", "history", lines=2))
        self.assertEqual(files["history.txt"], "two\nthree\n")
        self.assertTrue(json.loads(files["terminal.json"])["earlier_retained_lines_omitted"])

    async def test_file_is_binary_and_read_as_agent(self):
        files, _ = decode_observation(await self.term.get_file("/app/test.bin"))
        self.assertEqual(files["file.bin"], self.env.file_data)
        self.assertEqual(self.env.calls[-1]["user"], "agent")
        self.assertIn("set -o pipefail", self.env.calls[-1]["command"])

    async def test_file_permission_error(self):
        self.env.file_return_code = 44
        with self.assertRaises(RuntimeFault):
            await self.term.get_file("/private")

    async def test_file_size_error(self):
        self.term.settings = TerminalSettings(max_file_bytes=1)
        with self.assertRaises(RuntimeFault):
            await self.term.get_file("/large")

    async def test_32_mib_file_round_trip_and_oversized_rejection(self):
        size = 32 * 1024 * 1024
        self.env.file_data = bytes(range(256)) * (size // 256)
        expected_hash = hashlib.sha256(self.env.file_data).hexdigest()
        raw = await self.term.get_file("/app/limit.bin")
        frame = pack({"ok": True, "result": {"observation": raw}}, MAX_RESPONSE_BYTES)
        files, ended = decode_observation(loads(frame[4:])["result"]["observation"])
        self.assertFalse(ended)
        self.assertEqual(len(files["file.bin"]), size)
        self.assertEqual(hashlib.sha256(files["file.bin"]).hexdigest(), expected_hash)
        self.assertEqual(json.loads(files["file.json"]), {
            "path": "/app/limit.bin", "size": size, "sha256": expected_hash,
        })

        self.env.file_data += b"x"
        with self.assertRaisesRegex(RuntimeFault, "File exceeds configured retrieval limit") as raised:
            await self.term.get_file("/app/oversized.bin")
        self.assertEqual(raised.exception.code, "invalid_action")
        self.assertFalse((await self.term.read("t1"))["terminal"])

    async def test_dead_terminal_can_be_read_not_written(self):
        self.env.panes["%0"]["dead"] = 1
        files, _ = decode_observation(await self.term.read("t1"))
        self.assertTrue(json.loads(files["terminal.json"])["closed"])
        with self.assertRaises(RuntimeFault):
            await self.term.send("t1", [{"key": "Enter"}])
        await self.term.new()

    async def test_unknown_terminal_is_rejected(self):
        with self.assertRaises(RuntimeFault):
            await self.term.read("t99")

    async def test_missing_tmux_does_not_silently_run_host_terminal(self):
        env = ScriptedEnvironment()
        env.tmux_available = False
        terminal = TmuxTerminals(env, TerminalSettings(install_tmux=False))
        with self.assertRaises(RuntimeError):
            await terminal.setup()
        self.assertEqual(len(env.calls), 1)
