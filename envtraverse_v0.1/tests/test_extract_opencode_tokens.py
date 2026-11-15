from __future__ import annotations

from contextlib import redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis_scripts.extract_raw_cost_tokens import extract_raw_cost_tokens, extract_token_counts, main


def step(session="ses_first", message="msg_first", **tokens):
    return {
        "type": "step_finish", "sessionID": session,
        "part": {
            "id": "prt_first", "sessionID": session, "messageID": message,
            "type": "step-finish", "cost": 0, "tokens": tokens,
        },
    }


def usage_snapshot():
    return {
        "schema_version": 1, "source": "opencode.session_counters", "status": "collected",
        "collected_at": "2026-11-14T12:00:00+00:00", "game_alias": "game", "game": "counter",
        "totals": {"input_tokens": 202, "cached_input_tokens": 70, "output_tokens": 39},
        "tokens": {"input": 130, "output": 27, "reasoning": 12, "cache": {"read": 70, "write": 2}},
        "sessions": [
            {"id": "ses_root", "tokens": {"input": 100, "output": 20, "reasoning": 5,
                                           "cache": {"read": 30, "write": 2}}},
            {"id": "ses_child", "parent_id": "ses_root", "tokens": {
                "input": 10, "output": 3, "reasoning": 7, "cache": {"read": 40, "write": 0}}},
            {"id": "ses_recovery", "parent_id": None, "tokens": {
                "input": 20, "output": 4, "reasoning": 0, "cache": {"read": 0, "write": 0}}},
        ],
    }


class ExtractOpenCodeTokensTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.directory / "game" / "run" / "agent.log"
        self.path.parent.mkdir(parents=True)

    def write_events(self, *events):
        self.path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")

    def write_snapshot(self, snapshot=None):
        path = self.path.with_name("opencode_usage.json")
        path.write_text(json.dumps(usage_snapshot() if snapshot is None else snapshot), encoding="utf-8")
        return path

    def write_server_log(self):
        server_log = self.directory / "game" / "server" / "server.log"
        server_log.parent.mkdir(exist_ok=True)
        server_log.write_text(
            "2026-09-08T19:35:10+0000 INFO [environment-owner] env-server.server: "
            "started session token=test requested_game_id=game game_id=counter\n",
            encoding="utf-8",
        )

    def test_native_disjoint_token_categories_normalize_to_common_totals(self):
        self.write_events(step(input=100, output=20, reasoning=15, cache={"read": 300, "write": 40}))
        with redirect_stderr(io.StringIO()) as warnings:
            counts = extract_token_counts(self.path)
        self.assertEqual(counts, {"input_tokens": 440, "cached_input_tokens": 300, "output_tokens": 35})
        self.assertIn("no opencode_usage.json", warnings.getvalue())
        self.assertIn("root-only and incomplete", warnings.getvalue())

    def test_recorded_snapshot_includes_children_and_recovery_without_adding_stdout(self):
        # Even malformed stdout counters are irrelevant when a valid snapshot exists.
        self.write_events(step(input=-1, output=999))
        self.write_snapshot()
        with redirect_stderr(io.StringIO()) as warnings:
            self.assertEqual(extract_token_counts(self.path), usage_snapshot()["totals"])
        self.assertEqual(warnings.getvalue(), "")

    def test_snapshot_without_agent_log_preserves_existing_per_game_output_schema(self):
        self.write_server_log()
        self.write_snapshot()
        self.assertFalse(self.path.exists())
        self.assertEqual(extract_raw_cost_tokens(self.directory), [{
            **usage_snapshot()["totals"], "environment_id": "counter", "run_folder": str(self.directory / "game"),
        }])

    def test_empty_recorded_account_is_valid_and_does_not_fall_back_to_stdout(self):
        snapshot = usage_snapshot()
        snapshot["sessions"] = []
        snapshot["tokens"] = {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}
        snapshot["totals"] = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
        self.write_events(step(input=999, output=999))
        self.write_snapshot(snapshot)
        self.assertEqual(extract_token_counts(self.path), snapshot["totals"])

    def test_malformed_or_unsupported_snapshot_never_falls_back_to_valid_stdout(self):
        cases = [None, [], {"schema_version": 2}, {**usage_snapshot(), "schema_version": True}]
        for field in ("source", "status", "collected_at", "game_alias", "game", "tokens", "totals", "sessions"):
            snapshot = usage_snapshot()
            del snapshot[field]
            cases.append(snapshot)
        for field, value in (("status", "failed"), ("source", "stdout"),
                             ("collected_at", "invalid"), ("collected_at", "2026-11-14T12:00:00"),
                             ("collected_at", "2026-11-14T12:00:00+04:00"), ("sessions", {})):
            cases.append({**usage_snapshot(), field: value})
        self.write_events(step(input=100, output=20))
        path = self.path.with_name("opencode_usage.json")
        for raw in ["{not JSON", *(json.dumps(case) for case in cases)]:
            with self.subTest(raw=raw):
                path.write_text(raw, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "opencode_usage.json: invalid OpenCode usage snapshot"):
                    extract_token_counts(self.path)

    def test_snapshot_rejects_invalid_or_inconsistent_counters_and_duplicate_sessions(self):
        cases = []
        for value in (-1, True, 1.5, "100", None):
            for target in ("totals", "aggregate", "session"):
                snapshot = usage_snapshot()
                if target == "totals":
                    snapshot["totals"]["input_tokens"] = value
                elif target == "aggregate":
                    snapshot["tokens"]["cache"]["read"] = value
                else:
                    snapshot["sessions"][1]["tokens"]["reasoning"] = value
                cases.append(snapshot)
        snapshot = usage_snapshot()
        del snapshot["totals"]["output_tokens"]
        cases.append(snapshot)
        snapshot = usage_snapshot()
        snapshot["totals"]["input_tokens"] += 1
        cases.append(snapshot)
        snapshot = usage_snapshot()
        snapshot["tokens"]["input"] += 1
        cases.append(snapshot)
        snapshot = usage_snapshot()
        snapshot["sessions"].append(deepcopy(snapshot["sessions"][0]))
        cases.append(snapshot)
        snapshot = usage_snapshot()
        snapshot["sessions"][0]["id"] = ""
        cases.append(snapshot)
        self.write_events(step(input=100, output=20))
        for snapshot in cases:
            with self.subTest(snapshot=snapshot):
                self.write_snapshot(snapshot)
                with self.assertRaisesRegex(ValueError, "opencode_usage.json"):
                    extract_token_counts(self.path)

    def test_invalid_snapshot_preserves_previous_cli_output(self):
        self.write_server_log()
        self.write_events(step(input=100, output=20))
        self.write_snapshot({"schema_version": 2})
        output = self.directory / "raw_cost_tokens.json"
        output.write_text("previous output\n", encoding="utf-8")
        with redirect_stderr(io.StringIO()) as warnings:
            self.assertEqual(main([str(self.directory)]), 1)
        self.assertIn("opencode_usage.json", warnings.getvalue())
        self.assertEqual(output.read_text(encoding="utf-8"), "previous output\n")

    def test_open_code_artifacts_identify_incomplete_empty_stdout(self):
        self.write_events()
        self.path.with_name(".opencode_session_id").write_text("ses_root\n", encoding="utf-8")
        with redirect_stderr(io.StringIO()) as warnings:
            self.assertEqual(extract_token_counts(self.path), {
                "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
            })
        self.assertIn("root-only and incomplete", warnings.getvalue())

    def test_steps_are_summed_and_reconciliation_deduplicates_by_session_and_message(self):
        original = step(input=10, output=2)
        reconciled = step(input=11, output=3)
        reconciled["part"]["id"] = "prt_reconciled"
        self.write_events(
            original, step(message="msg_second", input=30, output=4),
            reconciled, step(session="ses_other", input=50, output=6), reconciled,
        )
        self.assertEqual(extract_token_counts(self.path), {
            "input_tokens": 91, "cached_input_tokens": 0, "output_tokens": 13,
        })

    def test_unrelated_and_malformed_steps_do_not_overwrite_valid_usage(self):
        self.write_events(
            step(input=100, output=20), {"type": "retry", "attempt": 2},
            {"type": "step_finish", "part": {}}, step(message=""),
            step(input=999, cache=None),
            {"type": "step_finish", "sessionID": "ses_first", "part": {"messageID": "msg_first"}},
        )
        with redirect_stderr(io.StringIO()) as warnings:
            counts = extract_token_counts(self.path)
        self.assertEqual(counts, {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 20})
        for line in (3, 4, 5, 6):
            self.assertIn(f"{self.path}:{line}:", warnings.getvalue())

    def test_invalid_native_token_counts_fail_with_location(self):
        for field in ("input", "output", "reasoning", "cache.read", "cache.write"):
            for value in (-1, True, 1.5, "100", None):
                with self.subTest(field=field, value=value):
                    tokens = {"cache": {}}
                    if field.startswith("cache."):
                        tokens["cache"][field.split(".")[1]] = value
                    else:
                        tokens[field] = value
                    self.write_events(step(**tokens))
                    with self.assertRaises(ValueError) as raised:
                        extract_token_counts(self.path)
                    self.assertIn(f"{self.path}:1: tokens.{field}", str(raised.exception))

    def test_codex_cumulative_counts_remain_independent_in_a_mixed_log(self):
        self.write_events(
            {"type": "thread.started", "thread_id": "codex_thread"},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 10}},
            step(input=30, reasoning=7, cache={"read": 20}),
            {"type": "thread.started", "thread_id": "codex_thread"},
            {"type": "turn.completed", "usage": {"input_tokens": 120, "output_tokens": 12}},
        )
        self.assertEqual(extract_token_counts(self.path), {
            "input_tokens": 170, "cached_input_tokens": 20, "output_tokens": 19,
        })

    def test_native_usage_is_included_in_existing_per_game_output(self):
        self.write_server_log()
        self.write_events(step(input=25, output=10, reasoning=5, cache={"read": 75}))
        self.assertEqual(extract_raw_cost_tokens(self.directory), [{
            "input_tokens": 100, "cached_input_tokens": 75, "output_tokens": 15,
            "environment_id": "counter", "run_folder": str(self.directory / "game"),
        }])


if __name__ == "__main__":
    unittest.main()
