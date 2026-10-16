from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis_scripts.extract_raw_actions import extract_raw_actions, main


PREFIX = "2026-09-08T19:35:10+0000 INFO [environment-owner] env-server.server: "
SCRIPT = ROOT / "analysis_scripts" / "extract_raw_actions.py"


def start(token: str, alias: str, environment_id: str) -> str:
    return f"started session token={token} requested_game_id={alias} game_id={environment_id}"


def action(token: str, payload: dict, step: int) -> str:
    return f"applied action session={token} action={payload!r} step_index={step}"


class ExtractRawActionsTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.run_folder = Path(temporary.name) / "run with spaces"
        self.run_folder.mkdir(parents=True)
        self.log_path = self.run_folder / "alias" / "server" / "server.log"

    def write_log(self, *messages: str, alias: str = "alias") -> None:
        self.log_path = self.run_folder / alias / "server" / "server.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text("".join(PREFIX + message + "\n" for message in messages), encoding="utf-8")

    def record(self, alias: str, environment_id: str, actions: list[dict]) -> dict:
        return {
            "actions": actions,
            "environment_id": environment_id,
            "run_folder": str(self.run_folder / alias),
        }

    def test_separate_servers_keep_real_ids_and_action_order(self) -> None:
        first = {"id": "ACTION1"}
        click = {"id": "ACTION6", "data": {"x": 19, "y": 20}}
        move = {"move": "north"}
        maze_id = "TRAINING/S0-keys/STAGE-01/0000.json"
        self.write_log(
            start("token-a", "alias-a", "bp35"),
            action("token-a", first, 1),
            action("token-a", click, 2),
            alias="alias-a",
        )
        self.write_log(start("token-c", "alias-c", "counter"), alias="alias-c")
        self.write_log(start("token-b", "alias-b", maze_id), action("token-b", move, 1), alias="alias-b")
        self.assertEqual(extract_raw_actions(self.run_folder), [
            self.record("alias-a", "bp35", [first, click]),
            self.record("alias-b", maze_id, [move]),
            self.record("alias-c", "counter", []),
        ])

    def test_same_environment_in_different_sessions_is_not_merged(self) -> None:
        self.write_log(
            start("token-a", "alias-a", "bp35"),
            action("token-a", {"id": "ACTION1"}, 1),
            alias="alias-a",
        )
        self.write_log(start("token-b", "alias-b", "bp35"), alias="alias-b")
        self.assertEqual(extract_raw_actions(self.run_folder), [
            self.record("alias-a", "bp35", [{"id": "ACTION1"}]),
            self.record("alias-b", "bp35", []),
        ])

    def test_action_cannot_use_a_session_from_another_server_log(self) -> None:
        self.write_log(start("a", "alias-a", "counter"), alias="alias-a")
        self.write_log(start("b", "alias-b", "counter"), action("a", {}, 1), alias="alias-b")
        with self.assertRaisesRegex(ValueError, "alias-b/server/server.log:2:.*unknown session"):
            extract_raw_actions(self.run_folder)

    def test_alias_must_match_its_server_folder(self) -> None:
        self.write_log(start("token", "wrong-alias", "counter"))
        with self.assertRaisesRegex(ValueError, "does not match its game folder"):
            extract_raw_actions(self.run_folder)

    def test_actions_before_session_start_are_rejected(self) -> None:
        self.write_log(action("token", {}, 1), start("token", "alias", "counter"))
        with self.assertRaisesRegex(ValueError, "server.log:1:.*unknown session"):
            extract_raw_actions(self.run_folder)

    def test_nested_values_and_log_like_strings_are_preserved(self) -> None:
        payload = {
            "action": "custom", "enabled": True, "other": False, "value": None,
            "data": [1, 2.5, {"text": "héllo\n'quoted' step_index=999"}],
            "message": "env-server.server: started session token=fake requested_game_id=fake game_id=fake",
        }
        self.write_log(start("token", "alias", "custom game"), action("token", payload, 1))
        self.assertEqual(extract_raw_actions(self.run_folder), [self.record("alias", "custom game", [payload])])

    def test_unrelated_logs_and_rejected_actions_are_ignored(self) -> None:
        self.write_log(
            "starting requested_game_id=failed-alias game_id=failed-game",
            start("token", "alias", "counter"),
            "POST /game/action from 127.0.0.1",
            "Invalid action: environment is terminal",
        )
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(PREFIX.replace("env-server.server", "env-server.environment")
                      + action("token", {"not": "a server action"}, 1) + "\n")
            log.write("Traceback (most recent call last):\n")
        self.assertEqual(extract_raw_actions(self.run_folder), [self.record("alias", "counter", [])])

    def test_empty_log_produces_empty_list(self) -> None:
        self.write_log()
        self.assertEqual(extract_raw_actions(self.run_folder), [])

    def test_malformed_or_unmatched_entries_fail_with_line_number(self) -> None:
        for message, expected in (
            ("started session token=missing-fields", "malformed session-start"),
            ("applied action session=token action={} step_index=bad", "malformed applied-action"),
            (action("missing-token", {}, 1), "unknown session token"),
            ("applied action session=token action={'id': step_index=1", "invalid action dictionary"),
            ("applied action session=token action=[] step_index=1", "action must be a dictionary"),
            (start("token", "alias", "counter"), "multiple sessions"),
        ):
            with self.subTest(message=message):
                self.write_log(start("token", "alias", "counter"), message)
                with self.assertRaisesRegex(ValueError, f"server.log:2:.*{expected}"):
                    extract_raw_actions(self.run_folder)

    def test_action_content_is_not_executed(self) -> None:
        marker = self.run_folder / "must-not-exist"
        expression = f"__import__('pathlib').Path({str(marker)!r}).touch()"
        self.write_log(
            start("token", "alias", "counter"),
            f"applied action session=token action={{'value': {expression}}} step_index=1",
        )
        with self.assertRaisesRegex(ValueError, "invalid action dictionary"):
            extract_raw_actions(self.run_folder)
        self.assertFalse(marker.exists())

    def test_cli_writes_json_into_input_folder_and_can_be_rerun(self) -> None:
        payload = {"id": "ACTION6", "data": {"x": 1, "y": 2}}
        self.write_log(start("token", "alias", "bp35"), action("token", payload, 1))
        expected = [self.record("alias", "bp35", [payload])]
        for _ in range(2):
            result = subprocess.run(
                [sys.executable, str(SCRIPT), self.run_folder.name],
                cwd=self.run_folder.parent, capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Saved 1 actions from 1 sessions", result.stdout)
            output = self.run_folder / "raw_actions.json"
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), expected)

    def test_missing_log_returns_error_without_creating_output(self) -> None:
        with redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main([str(self.run_folder)]), 1)
        self.assertIn("server.log", stderr.getvalue())
        self.assertFalse((self.run_folder / "raw_actions.json").exists())

    def test_parse_error_preserves_previous_output(self) -> None:
        output = self.run_folder / "raw_actions.json"
        output.write_text("previous output\n", encoding="utf-8")
        self.write_log(action("unknown", {}, 1))
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(main([str(self.run_folder)]), 1)
        self.assertEqual(output.read_text(encoding="utf-8"), "previous output\n")

if __name__ == "__main__":
    unittest.main()
