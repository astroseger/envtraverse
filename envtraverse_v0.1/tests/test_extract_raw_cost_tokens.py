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

from analysis_scripts.extract_raw_actions import extract_raw_actions
from analysis_scripts.extract_raw_cost_tokens import TOKEN_FIELDS, extract_raw_cost_tokens, extract_token_counts, main


PREFIX = "2026-09-08T19:35:10+0000 INFO [environment-owner] env-server.server: "
SCRIPT = ROOT / "analysis_scripts" / "extract_raw_cost_tokens.py"


def thread(thread_id: str) -> dict:
    return {"type": "thread.started", "thread_id": thread_id}


def completed(input_tokens: int, cached_input_tokens: int, output_tokens: int) -> dict:
    return {"type": "turn.completed", "usage": dict(zip(TOKEN_FIELDS, (input_tokens, cached_input_tokens, output_tokens)))}


class ExtractRawCostTokensTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.run_folder = Path(temporary) / "run with spaces"
        (self.run_folder / "alias" / "server").mkdir(parents=True)
        self.server_log = self.run_folder / "alias" / "server" / "server.log"
        self.server_log.write_text("", encoding="utf-8")

    def add_session(self, token: str, alias: str, environment_id: str) -> None:
        path = self.run_folder / alias / "server" / "server.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as log:
            log.write(PREFIX + f"started session token={token} requested_game_id={alias} game_id={environment_id}\n")

    def agent_log(self, alias: str, *events: dict | str) -> Path:
        path = self.run_folder / alias / "run" / "agent.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join((event if isinstance(event, str) else json.dumps(event)) + "\n" for event in events), encoding="utf-8")
        return path

    def test_latest_counts_per_thread_are_summed_without_double_counting_resumes(self) -> None:
        path = self.agent_log(
            "alias", "2026-09-09 INFO starting agent", thread("first"),
            completed(100, 80, 10), thread("first"), completed(250, 190, 35),
            thread("second"), completed(40, 20, 8), thread("first"), completed(300, 230, 50),
            thread("first"), {"type": "turn.failed", "error": {"message": "interrupted"}},
        )
        self.assertEqual(extract_token_counts(path), {
            "input_tokens": 340, "cached_input_tokens": 250, "output_tokens": 58,
        })

    def test_different_sessions_and_environments_follow_raw_actions_order_and_paths(self) -> None:
        sessions = (("b", "alias-b", "counter"), ("a", "alias-a", "TRAINING/S0-keys/STAGE-01/0000.json"), ("c", "alias-c", "counter"))
        for token, alias, environment_id in sessions:
            self.add_session(token, alias, environment_id)
            self.agent_log(alias, thread("same-thread-id-in-another-log"), completed(100, 80, 10))
        with (self.run_folder / "alias-b/server/server.log").open("a", encoding="utf-8") as log:
            log.write(PREFIX + "applied action session=b action={'id': 'INC'} step_index=1\n")
        records = extract_raw_cost_tokens(self.run_folder)
        self.assertEqual(records, [
            {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10,
             "environment_id": item["environment_id"], "run_folder": item["run_folder"]}
            for item in extract_raw_actions(self.run_folder)
        ])
        self.assertFalse((self.run_folder / "raw_actions.json").exists())

    def test_only_token_fields_are_extracted_from_usage(self) -> None:
        event = completed(100, 80, 10)
        event["usage"].update(reasoning_output_tokens=7, cache_write_input_tokens=0)
        path = self.agent_log("alias", thread("first"), event)
        self.assertEqual(extract_token_counts(path), dict(zip(TOKEN_FIELDS, (100, 80, 10))))

    def test_empty_log_and_thread_without_completed_usage_warn_and_return_zero(self) -> None:
        for events in ((), (thread("first"),), ("2026-09-09 ERROR initialization failed",)):
            with self.subTest(events=events):
                path = self.agent_log("alias", *events)
                with redirect_stderr(io.StringIO()) as stderr:
                    counts = extract_token_counts(path)
                self.assertEqual(counts, dict.fromkeys(TOKEN_FIELDS, 0))
                self.assertIn(str(path), stderr.getvalue())
                self.assertIn("counting zero tokens", stderr.getvalue())

    def test_malformed_and_unrelated_lines_do_not_overwrite_valid_usage(self) -> None:
        path = self.agent_log(
            "alias", thread("first"), completed(100, 80, 10),
            "regular agent logging", "[]", {"type": "item.completed", "item": {"text": "turn.completed"}},
            '{"type":"turn.completed",', {"type": "turn.completed", "usage": None},
        )
        with redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(extract_token_counts(path), dict(zip(TOKEN_FIELDS, (100, 80, 10))))
        self.assertIn(f"{path}:6:", stderr.getvalue())
        self.assertIn(f"{path}:7:", stderr.getvalue())

    def test_missing_thread_id_does_not_attribute_usage_to_the_previous_thread(self) -> None:
        path = self.agent_log(
            "alias", completed(999, 999, 999), thread("first"), completed(100, 80, 10),
            {"type": "thread.started"}, completed(999, 999, 999),
        )
        with redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(extract_token_counts(path), dict(zip(TOKEN_FIELDS, (100, 80, 10))))
        self.assertIn("without active thread", stderr.getvalue())
        self.assertIn("without thread_id", stderr.getvalue())

    def test_missing_token_fields_default_to_zero(self) -> None:
        path = self.agent_log("alias", thread("first"), {"type": "turn.completed", "usage": {"output_tokens": 12}})
        self.assertEqual(extract_token_counts(path), dict(zip(TOKEN_FIELDS, (0, 0, 12))))

    def test_invalid_token_counts_fail_with_location(self) -> None:
        for value in (-1, True, 1.5, "100", None):
            with self.subTest(value=value):
                event = completed(100, 80, 10)
                event["usage"]["input_tokens"] = value
                path = self.agent_log("alias", thread("first"), event)
                with self.assertRaisesRegex(ValueError, "agent.log:2: input_tokens must be a non-negative integer"):
                    extract_token_counts(path)

    def test_empty_server_log_produces_empty_list(self) -> None:
        self.assertEqual(extract_raw_cost_tokens(self.run_folder), [])

    def test_unrelated_server_messages_and_unstarted_folders_are_ignored(self) -> None:
        self.agent_log("not-started", thread("thread"), completed(100, 80, 10))
        self.server_log.write_text(
            PREFIX + "starting server environment=example host=0.0.0.0 port=8879\n"
            + "Traceback (most recent call last):\n"
            + PREFIX.replace("env-server.server", "env-server.environment")
            + "starting requested_game_id=not-started game_id=counter\n"
            + PREFIX.replace("env-server.server", "env-server.environment")
            + "started session token=fake requested_game_id=not-started game_id=counter\n",
            encoding="utf-8",
        )
        self.assertEqual(extract_raw_cost_tokens(self.run_folder), [])

    def test_failed_setup_has_a_zero_token_record_and_no_action_session(self) -> None:
        self.server_log.write_text(
            PREFIX + "starting requested_game_id=alias game_id=terminal-bench-2.1-fix-ocaml-gc\n"
            + PREFIX + "request rejected: Task preparation failed\n", encoding="utf-8",
        )
        self.agent_log("alias", {"type": "external_agent_log", "message": "initialization failed"})
        with redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(extract_raw_cost_tokens(self.run_folder), [{
                **dict.fromkeys(TOKEN_FIELDS, 0),
                "environment_id": "terminal-bench-2.1-fix-ocaml-gc",
                "run_folder": str(self.run_folder / "alias"),
            }])
        self.assertIn("no thread usage found; counting zero tokens", stderr.getvalue())
        self.assertEqual(extract_raw_actions(self.run_folder), [])

    def test_start_attempt_uses_actual_usage_instead_of_assuming_zero(self) -> None:
        self.server_log.write_text(PREFIX + "starting requested_game_id=alias game_id=counter\n", encoding="utf-8")
        path = self.agent_log("alias", thread("first"), completed(100, 80, 10))
        self.assertEqual(extract_raw_cost_tokens(self.run_folder), [{
            "input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10,
            "environment_id": "counter", "run_folder": str(self.run_folder / "alias"),
        }])
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            extract_raw_cost_tokens(self.run_folder)

    def test_successful_start_with_an_attempt_entry_is_counted_once(self) -> None:
        self.server_log.write_text(PREFIX + "starting requested_game_id=alias game_id=counter\n", encoding="utf-8")
        self.add_session("token", "alias", "counter")
        self.agent_log("alias", thread("first"), completed(100, 80, 10))
        records = extract_raw_cost_tokens(self.run_folder)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["environment_id"], "counter")
        self.assertEqual(records[0]["input_tokens"], 100)

    def test_malformed_duplicate_or_unsafe_start_attempts_fail(self) -> None:
        self.agent_log("alias", thread("first"), completed(100, 80, 10))
        valid = "starting requested_game_id=alias game_id=counter"
        for messages, expected in (
            (["starting requested_game_id=alias"], "malformed start-attempt"),
            ([valid, valid], "multiple start attempts"),
            (["starting requested_game_id=../outside game_id=counter"], "must be a folder name"),
            (["starting requested_game_id=other game_id=counter"], "does not match its game folder"),
        ):
            with self.subTest(messages=messages):
                self.server_log.write_text("".join(PREFIX + message + "\n" for message in messages), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, f"server.log:[0-9]+:.*{expected}"):
                    extract_raw_cost_tokens(self.run_folder)

    def test_malformed_duplicate_or_unsafe_session_entries_fail(self) -> None:
        self.agent_log("alias", thread("first"), completed(100, 80, 10))
        for message, expected in (
            ("started session token=bad", "malformed session-start"),
            ("started session token=first requested_game_id=alias game_id=counter", "multiple sessions"),
            ("started session token=other requested_game_id=../outside game_id=counter", "must be a folder name"),
        ):
            with self.subTest(message=message):
                self.server_log.write_text("", encoding="utf-8")
                self.add_session("first", "alias", "counter")
                with self.server_log.open("a", encoding="utf-8") as log:
                    log.write(PREFIX + message + "\n")
                with self.assertRaisesRegex(ValueError, f"server.log:2:.*{expected}"):
                    extract_raw_cost_tokens(self.run_folder)

    def test_cli_writes_json_and_can_be_rerun_from_a_different_directory(self) -> None:
        self.add_session("first", "alias", "custom გარემო")
        self.agent_log("alias", thread("first"), completed(100, 80, 10))
        for _ in range(2):
            result = subprocess.run(
                [sys.executable, str(SCRIPT), self.run_folder.name],
                cwd=self.run_folder.parent, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Saved token counts from 1 sessions", result.stdout)
            output = json.loads((self.run_folder / "raw_cost_tokens.json").read_text(encoding="utf-8"))
            self.assertEqual(output, [{
                "input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10,
                "environment_id": "custom გარემო", "run_folder": str(self.run_folder / "alias"),
            }])

    def test_missing_log_and_invalid_counts_preserve_previous_output(self) -> None:
        self.add_session("first", "alias", "counter")
        output = self.run_folder / "raw_cost_tokens.json"
        output.write_text("previous output\n", encoding="utf-8")
        with redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main([str(self.run_folder)]), 1)
        self.assertIn("agent.log", stderr.getvalue())
        self.assertEqual(output.read_text(encoding="utf-8"), "previous output\n")
        self.agent_log("alias", thread("first"), completed(-1, 0, 0))
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(main([str(self.run_folder)]), 1)
        self.assertEqual(output.read_text(encoding="utf-8"), "previous output\n")

    def test_missing_server_log_returns_error_without_creating_output(self) -> None:
        self.server_log.unlink()
        with redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main([str(self.run_folder)]), 1)
        self.assertIn("server.log", stderr.getvalue())
        self.assertFalse((self.run_folder / "raw_cost_tokens.json").exists())


if __name__ == "__main__":
    unittest.main()
