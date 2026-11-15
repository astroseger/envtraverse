from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.controller import opencode_usage as usage


def tokens(value=1):
    return {"input": value, "output": value * 2, "reasoning": value * 3,
            "cache": {"read": value * 4, "write": value * 5}}


def session(identifier, value=1, parent=None):
    result = {"id": identifier, "tokens": tokens(value), "title": "private title",
              "metadata": {"private": "must not export"}}
    if parent is not None:
        result["parentID"] = parent
    return result


def page(sessions, cursor=None):
    return json.dumps({"data": sessions, "cursor": {"next": cursor, "previous": None}}).encode()


class OpenCodeUsageCLITest(unittest.TestCase):
    def test_relocated_usage_cli_help_works_from_outside_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
            result = subprocess.run(
                [sys.executable, str(ROOT / "src/controller/opencode_usage.py"), "--help"],
                cwd=directory, env=environment, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Recover pending per-game OpenCode usage", result.stdout)
            self.assertEqual(list(directory.iterdir()), [])


class OpenCodeUsageTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.account = self.directory / "account"
        self.account.mkdir()
        (self.account / "opencode.db").touch()
        self.output = self.directory / "game/run/opencode_usage.json"
        self.docker = self.enterContext(patch.object(usage.account_tools, "Docker")).return_value

    def collect(self):
        return usage.collect_account_usage(self.account, self.output, image="test-image", game_alias="alias", game="counter")

    def test_all_pages_unique_sessions_and_disjoint_categories(self):
        root = session("root", 1)
        self.docker.run.side_effect = [
            page([root, session("child", 2, "root")], "page2"),
            page([root, session("nested", 3, "child"), session("recovery", 4)], "empty"),
            page([]),
        ]
        snapshot = self.collect()
        self.assertEqual(snapshot["tokens"], tokens(10))
        self.assertEqual(snapshot["totals"], {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 50})
        self.assertEqual({s["id"] for s in snapshot["sessions"]}, {"root", "child", "nested", "recovery"})
        self.assertEqual(snapshot["source"], "opencode.session_counters")
        self.assertEqual(snapshot["status"], "collected")
        self.assertEqual(json.loads(self.output.read_text()), snapshot)
        self.assertNotIn("private", self.output.read_text())
        self.assertEqual(len(self.docker.run.call_args_list), 3)
        for call in self.docker.run.call_args_list:
            self.assertEqual(call.args[0], self.account)
            self.assertTrue(call.kwargs["offline"])
            self.assertTrue(call.kwargs["isolate"])
            self.assertNotIn("parentID=null", call.args[1])
        self.assertIn("cursor=page2", self.docker.run.call_args_list[1].args[1])
        self.assertIn("cursor=empty", self.docker.run.call_args_list[2].args[1])

    def test_empty_account_produces_explicit_zero_snapshot(self):
        self.docker.run.return_value = page([])
        snapshot = self.collect()
        self.assertEqual(snapshot["sessions"], [])
        self.assertEqual(snapshot["tokens"], tokens(0))
        self.assertEqual(snapshot["totals"], {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0})

    def test_invalid_game_metadata_cannot_create_unreadable_snapshot(self):
        for name in ("game_alias", "game"):
            for value in (None, "", " ", 7):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    usage.collect_account_usage(self.account, self.output, image="test-image",
                                                **{**{"game_alias": "alias", "game": "counter"}, name: value})
        self.docker.run.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_invalid_pages_leave_existing_snapshot_unchanged(self):
        self.output.parent.mkdir(parents=True)
        self.output.write_text("previous good snapshot")
        invalid = [b"not json", b"[]", b'{"data":[]}', page([{}]), page([session(" ")]),
                   page([session("root", parent=3)])]
        for key in ("input", "output", "reasoning"):
            broken = session("root")
            broken["tokens"][key] = None
            invalid.append(page([broken]))
        for value in (True, -1, 1.5, "1"):
            broken = session("root")
            broken["tokens"]["cache"]["read"] = value
            invalid.append(page([broken]))
        for response in invalid:
            with self.subTest(response=response):
                self.docker.run.return_value = response
                with self.assertRaises(ValueError):
                    self.collect()
                self.assertEqual(self.output.read_text(), "previous good snapshot")

    def test_repeated_cursor_and_changed_session_fail_without_snapshot(self):
        for responses in ([page([], "same"), page([], "same")],
                          [page([session("root")], "next"), page([session("root", 2)])]):
            with self.subTest(responses=responses):
                self.docker.run.side_effect = responses
                with self.assertRaises(ValueError):
                    self.collect()
                self.assertFalse(self.output.exists())

    def test_failed_collection_or_replace_keeps_old_snapshot(self):
        self.output.parent.mkdir(parents=True)
        self.output.write_text("previous")
        self.docker.run.side_effect = usage.account_tools.AccountError("offline API failed")
        with self.assertRaises(usage.account_tools.AccountError):
            self.collect()
        self.assertEqual(self.output.read_text(), "previous")
        self.docker.run.side_effect = None
        self.docker.run.return_value = page([session("root")])
        with patch.object(usage.os, "replace", side_effect=OSError("disk error")), self.assertRaises(OSError):
            self.collect()
        self.assertEqual(self.output.read_text(), "previous")
        self.assertEqual(list(self.output.parent.iterdir()), [self.output])

    def write_pending(self):
        self.marker = usage.account_tools.pending_usage_path(self.account)
        self.pending = {"schema_version": 1, "account": str(self.account), "game_alias": "alias",
                        "game": "counter", "output_path": str(self.output), "image": "old-image",
                        "container_name": "old-agent"}
        self.marker.write_text(json.dumps(self.pending))

    def test_pending_recovery_holds_lock_and_clears_marker_after_success(self):
        self.write_pending()
        self.docker.run.return_value = page([session("root")])
        def stopped(*args, **kwargs):
            with self.assertRaises(usage.account_tools.AccountError):
                with usage.account_tools.account_lock(self.account):
                    self.fail("Recovery did not lock account")
            return Mock(stdout="unrelated-container\trunning\nold-agent\texited\n")
        with patch.object(usage.subprocess, "run", side_effect=stopped):
            self.assertEqual(usage.recover_pending_usage(self.account, image="replacement-image"), self.output)
        self.assertEqual(json.loads(self.output.read_text())["image"], "replacement-image")
        self.assertFalse(self.marker.exists())

    def test_active_container_and_failed_recovery_preserve_pending_marker(self):
        self.write_pending()
        for state in ("created", "running", "paused", "restarting", "removing", "dead"):
            with self.subTest(state=state), \
                 patch.object(usage.subprocess, "run", return_value=Mock(stdout=f"old-agent\t{state}\n")), \
                 self.assertRaisesRegex(RuntimeError, "Stop container"):
                usage.recover_pending_usage(self.account)
        self.docker.run.assert_not_called()
        self.docker.run.side_effect = usage.account_tools.AccountError("collection failed")
        with patch.object(usage.subprocess, "run", return_value=Mock(stdout="")), \
             self.assertRaises(usage.account_tools.AccountError):
            usage.recover_pending_usage(self.account)
        self.assertTrue(self.marker.exists())
        self.assertFalse(self.output.exists())

    def test_invalid_marker_cannot_collect_another_account_or_write_arbitrary_filename(self):
        self.write_pending()
        for key, value in (("account", "/different/account"), ("schema_version", True),
                           ("container_name", None), ("output_path", "/tmp/arbitrary.json")):
            with self.subTest(key=key):
                modified = {**self.pending, key: value}
                self.marker.write_text(json.dumps(modified))
                with patch.object(usage.subprocess, "run") as run, self.assertRaises(ValueError):
                    usage.recover_pending_usage(self.account)
                run.assert_not_called()
                self.assertTrue(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
