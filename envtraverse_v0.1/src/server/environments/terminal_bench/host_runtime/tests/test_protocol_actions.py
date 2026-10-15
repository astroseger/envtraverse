from __future__ import annotations

import json
import struct
import unittest
from unittest.mock import patch

from terminal_bench.actions import validate_action
from terminal_bench.description import environment_description
from terminal_bench.protocol import (MAX_RESPONSE_BYTES, RuntimeClient, RuntimeFault,
                                     decode_observation, loads, observation, pack)
from terminal_bench.settings import TerminalSettings


class ProtocolTests(unittest.TestCase):
    def test_binary_and_text_round_trip(self):
        files = {"text.txt": "a\nΩ\x00", "bytes.bin": b"\0\xff\n"}
        decoded, ended = decode_observation(json.loads(json.dumps(observation(files))))
        self.assertEqual(decoded, files)
        self.assertFalse(ended)

    def test_flat_names_only(self):
        for name in ["../x", "/x", ".", "..", "x\\y", "a\0b", ""]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                observation({name: "x"})

    def test_bad_encoded_content_rejected(self):
        bad = {"files": {"file.bin": {"base64": "!!!"}}, "terminal": False}
        with self.assertRaises(ValueError):
            decode_observation(bad)

    def test_invalid_json_objects_and_nan(self):
        for raw in [b"[]", b"42", b'{"x":NaN}']:
            with self.assertRaises(ValueError):
                loads(raw)

    def test_frame_size_bound(self):
        with self.assertRaises(ValueError):
            pack({"data": "x" * 1000}, 10)

    def test_invalid_request_encoding_does_not_connect(self):
        for value in (float("nan"), object()):
            with self.subTest(value=type(value).__name__), patch(
                "terminal_bench.protocol.socket.socket"
            ) as socket_factory:
                with self.assertRaises(RuntimeFault) as raised:
                    RuntimeClient("/unused.sock").call("action", action={"value": value})
                self.assertEqual(raised.exception.code, "bad_request")
                socket_factory.assert_not_called()

    def test_bad_responses_are_not_classified_as_invalid_requests(self):
        replies = [
            [struct.pack("!I", MAX_RESPONSE_BYTES + 1)],
            [struct.pack("!I", 2), b"[]"],
        ]
        for chunks in replies:
            with self.subTest(chunks=chunks), patch(
                "terminal_bench.protocol.socket.socket"
            ) as socket_factory:
                connection = socket_factory.return_value.__enter__.return_value
                connection.recv.side_effect = chunks
                with self.assertRaises(ValueError):
                    RuntimeClient("/unused.sock").call("health")
                connection.sendall.assert_called_once()

    def test_instruction_is_preserved_not_summarized(self):
        text = "A task with exact  whitespace.\n\nWrite `Ω`.\nHarbor / Terminal-Bench in task text.\n"
        prompt = environment_description(text, 100)
        self.assertIn("--- BEGIN TASK ---\n" + text + "\n--- END TASK ---", prompt)
        self.assertNotIn("solve.sh", prompt)
        self.assertNotIn("test.sh", prompt)

    def test_description_omits_backend_names_and_private_metadata(self):
        prompt = environment_description("PUBLIC TASK", 100, time_budget={
            "agent_timeout_seconds": 900,
            "deadline_at": "PRIVATE DEADLINE",
            "clock_note": "PRIVATE CLOCK NOTE",
            "remaining_seconds": 899,
        })
        self.assertIn("Solving time budget: 900 seconds (wall-clock).", prompt)
        for private in ("harbor", "terminal-bench", "terminal_bench", "grading",
                        "scores", "private", "time_budget.json", "deadline", "remaining"):
            with self.subTest(private=private):
                self.assertNotIn(private, prompt.lower())

    def test_description_examples_are_valid_actions(self):
        prompt = environment_description("Task.", 100)
        for line in prompt.splitlines():
            if line.startswith('- {"id":'):
                validate_action(json.loads(line[2:]))

    def test_validate_all_actions(self):
        for action in [
            {"id": "terminal_new"}, {"id": "finish"},
            {"id": "terminal_read", "terminal_id": "t1"},
            {"id": "terminal_read", "terminal_id": "t2", "view": "history", "lines": 12, "wait_ms": 0},
            {"id": "get_file", "path": "/tmp/a b'c"},
            {"id": "send_path", "path_local": "folder/a b'Ω", "path_sandbox": "/app/a b'Ω"},
            {"id": "send_path", "path_local": ".", "path_sandbox": "/app/workspace"},
            {"id": "terminal_send", "terminal_id": "t1", "keys": [{"text": "C-c"}, {"key": "C-c"}]},
        ]:
            self.assertEqual(validate_action(action), action)

    def test_reject_bad_actions(self):
        for action in [
            [], {}, {"id": "oops"}, {"id": "finish", "extra": 1},
            {"id": "terminal_send", "terminal_id": "t1", "keys": [{"text": "x", "key": "Enter"}]},
            {"id": "terminal_send", "terminal_id": "t1", "keys": [{"key": "run-shell"}]},
            {"id": "terminal_send", "terminal_id": "t1", "keys": [{"text": "\0"}]},
            {"id": "terminal_send", "terminal_id": "t1", "keys": [{"text": "\ud800"}]},
            {"id": "terminal_read", "terminal_id": "t1", "wait_ms": True},
            {"id": "terminal_read", "terminal_id": "t1", "wait_ms": 999999},
            {"id": "terminal_read", "terminal_id": "t1", "lines": 0},
            {"id": "get_file", "path": "relative"},
            {"id": "terminal_read", "terminal_id": "; kill-server"},
        ]:
            with self.subTest(action=action), self.assertRaises(RuntimeFault):
                validate_action(action)

    def test_reject_invalid_upload_paths_and_fields(self):
        valid = {"id": "send_path", "path_local": "source", "path_sandbox": "/app/target"}
        cases = [valid | {"extra": True}, {"id": "send_path"}]
        for name in ("path_local", "path_sandbox"):
            cases.extend(valid | {name: value} for value in (None, 1, "", "a\0b", "\ud800", "x" * 4097))
        cases.extend(valid | {"path_local": value} for value in ("/etc/passwd", "../other", "a/../../other"))
        cases.append(valid | {"path_sandbox": "relative"})
        for action in cases:
            with self.subTest(action=repr(action)[:100]), self.assertRaises(RuntimeFault):
                validate_action(action)

    def test_settings_validation(self):
        for kwargs in [{"columns": 0}, {"rows": True}, {"install_tmux": "yes"}]:
            with self.assertRaises(ValueError):
                TerminalSettings(**kwargs)
