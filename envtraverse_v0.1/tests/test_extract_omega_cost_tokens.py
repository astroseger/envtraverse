from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis_scripts.extract_raw_cost_tokens import (
    TOKEN_FIELDS, detect_log_format, extract_agent_token_counts,
    extract_raw_cost_tokens, extract_token_counts, main,
)


OMEGA_PREFIX = "2026-11-10 15:23:47 | INFO     | lib_llm_ext | [LLM_USAGE] provider=ChatGPT "
SERVER_PREFIX = "2026-11-10T15:23:30+0000 INFO [environment-owner] env-server.server: "


def usage(input_tokens=100, cached_tokens=80, output_tokens=10) -> str:
    return (OMEGA_PREFIX + "model=test-model status=completed incomplete_reason=None "
            f"input_tokens={input_tokens} cached_tokens={cached_tokens} "
            f"output_tokens={output_tokens} reasoning_tokens=7")


def completed(input_tokens=100, cached_input_tokens=80, output_tokens=10) -> dict:
    return {"type": "turn.completed", "usage": dict(zip(
        TOKEN_FIELDS, (input_tokens, cached_input_tokens, output_tokens),
    ))}


class ExtractOmegaCostTokensTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.run = self.root / "run"

    def agent_log(self, *events, alias="alias"):
        path = self.run / alias / "run/agent.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(
            (event if isinstance(event, str) else json.dumps(event)) + "\n"
            for event in events
        ), encoding="utf-8")
        return path

    def start_game(self, alias, game="counter", *, failed_setup=False):
        path = self.run / alias / "server/server.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        message = "starting" if failed_setup else "started session token=fixture"
        path.write_text(SERVER_PREFIX + f"{message} requested_game_id={alias} game_id={game}\n")

    def test_sum_responses_including_identical_records_without_adding_subsets(self):
        path = self.agent_log(usage(), usage(), usage(50, 0, 8))
        self.assertEqual(detect_log_format(path), "omega_chatgpt")
        self.assertEqual(extract_agent_token_counts(path), {
            "input_tokens": 250, "cached_input_tokens": 160, "output_tokens": 28,
        })

    def test_codex_precedence_preserves_results_warnings_and_errors(self):
        cases = [
            [{"type": "thread.started", "thread_id": "first"}, completed(),
             {"type": "thread.started", "thread_id": "first"}, completed(250, 190, 35)],
            [{"type": "thread.started", "thread_id": "first"}, completed(0, 0, 0)],
            [{"type": "thread.started", "thread_id": "first"}],
            [completed()],
            [{"type": "thread.started"}, completed()],
            [{"type": "thread.started", "thread_id": "first"}, completed(-1)],
            [{"type": "thread.started", "thread_id": "first"}, '{"type":"turn.completed",'],
        ]
        for events in cases:
            with self.subTest(events=events):
                # Invalid Omega data must not override any recognized Codex stream.
                path = self.agent_log(usage(-999), *events, usage())
                self.assertEqual(detect_log_format(path), "codex")
                results = []
                for parser in (extract_token_counts, extract_agent_token_counts):
                    errors = io.StringIO()
                    with redirect_stderr(errors):
                        try:
                            result = parser(path)
                        except ValueError as error:
                            result = (type(error), str(error))
                    results.append((result, errors.getvalue()))
                self.assertEqual(*results)

    def test_quoted_embedded_and_other_provider_usage_is_ignored(self):
        record = usage(999, 900, 99)
        path = self.agent_log(
            json.dumps(record), {"type": "item.completed", "text": record},
            "2026-11-10 15:23:47 | INFO | loop | (REQUEST: " + record + ")",
            record.replace("lib_llm_ext", "loop"),
            record.replace("provider=ChatGPT", "provider=OpenAI"),
            record.replace("provider=ChatGPT", "provider=ChatGPTExtra"),
            record.split("[LLM_USAGE]", 1)[1], usage(),
        )
        self.assertEqual(extract_agent_token_counts(path), dict(zip(TOKEN_FIELDS, (100, 80, 10))))

    def test_unreported_failed_and_inflight_requests_add_no_usage(self):
        path = self.agent_log(
            "2026-11-10 15:23:40 | INFO | loop | (REQUEST: first)", usage(),
            "2026-11-10 15:23:49 | INFO | loop | (RESPONSE: ChatGPT stream ended before completion)",
            "2026-11-10 15:23:50 | INFO | loop | (REQUEST: interrupted)",
        )
        self.assertEqual(extract_agent_token_counts(path), dict(zip(TOKEN_FIELDS, (100, 80, 10))))

    def test_missing_and_none_fields_warn_and_keep_available_counts(self):
        path = self.agent_log(usage(cached_tokens=None), OMEGA_PREFIX + "output_tokens=5")
        with redirect_stderr(io.StringIO()) as errors:
            counts = extract_agent_token_counts(path)
        self.assertEqual(counts, dict(zip(TOKEN_FIELDS, (100, 0, 15))))
        for message in (":1: Omega cached_tokens unavailable", ":2: Omega input_tokens unavailable",
                        ":2: Omega cached_tokens unavailable"):
            self.assertIn(message, errors.getvalue())

    def test_invalid_counters_do_not_fall_back_and_preserve_previous_output(self):
        self.start_game("alias")
        output = self.run / "raw_cost_tokens.json"
        output.write_text("previous output\n")
        for value in (-1, True, 1.5, "invalid", ""):
            with self.subTest(value=value):
                path = self.agent_log(usage(input_tokens=value))
                self.assertEqual(detect_log_format(path), "omega_chatgpt")
                with redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(main([str(self.run)]), 1)
                self.assertIn("agent.log:1: input_tokens must be a non-negative integer", errors.getvalue())
                self.assertEqual(output.read_text(), "previous output\n")

    def test_duplicate_or_malformed_usage_fields_are_errors(self):
        for suffix in (" input_tokens=500", " broken-field"):
            with self.subTest(suffix=suffix):
                path = self.agent_log(usage() + suffix)
                with self.assertRaisesRegex(ValueError, "agent.log:1: malformed or duplicate Omega usage field"):
                    extract_agent_token_counts(path)

    def test_unknown_formats_preserve_existing_behavior(self):
        for events in ((), ("initialization failed",), ('{"broken":',),
                       (usage().replace("provider=ChatGPT", "provider=OpenAI"),)):
            with self.subTest(events=events):
                path = self.agent_log(*events)
                self.assertEqual(detect_log_format(path), "unknown")
                results = []
                for parser in (extract_token_counts, extract_agent_token_counts):
                    with redirect_stderr(io.StringIO()) as errors:
                        counts = parser(path)
                    results.append((counts, errors.getvalue()))
                self.assertEqual(*results)

    def test_mixed_games_keep_schema_order_and_ignore_console_mirror(self):
        self.start_game("b-codex", "codex-game")
        self.agent_log({"type": "thread.started", "thread_id": "first"}, completed(), alias="b-codex")
        self.start_game("a-omega")
        omega_log = self.agent_log(usage(), usage(50, 0, 8), alias="a-omega")
        omega_log.with_name("omega_console.log").write_text(omega_log.read_text())
        self.agent_log(usage(999), alias="unstarted")
        expected = [
            {"input_tokens": 150, "cached_input_tokens": 80, "output_tokens": 18,
             "environment_id": "counter", "run_folder": str(self.run / "a-omega")},
            {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10,
             "environment_id": "codex-game", "run_folder": str(self.run / "b-codex")},
        ]
        self.assertEqual(extract_raw_cost_tokens(self.run), expected)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main([str(self.run)]), 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(json.loads((self.run / "raw_cost_tokens.json").read_text()), expected)

    def test_failed_setup_retains_reported_omega_usage(self):
        self.start_game("alias", failed_setup=True)
        self.agent_log(usage())
        self.assertEqual(extract_raw_cost_tokens(self.run), [{
            "input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 10,
            "environment_id": "counter", "run_folder": str(self.run / "alias"),
        }])


if __name__ == "__main__":
    unittest.main()
