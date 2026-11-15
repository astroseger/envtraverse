"""Opt-in API smoke with a temporary database, no credentials or model requests.

Run with OPENCODE_USAGE_DOCKER_TESTS=1 after building the pinned account image.
Every container has networking disabled. Counter fixtures are written only after
the CLI has exited, and only into this test's newly created database.
"""

from __future__ import annotations

from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from opencode_chatgpt_accounts import account_tools as accounts

IMAGE = f"opencode-chatgpt-accounts:2.3.0-uid{os.getuid()}-gid{os.getgid()}"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(
    os.environ.get("OPENCODE_USAGE_DOCKER_TESTS") == "1",
    "Set OPENCODE_USAGE_DOCKER_TESTS=1 for the offline synthetic Docker smoke",
)
class OpenCodeUsageIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory(
            prefix="opencode-usage-integration-",
        )))
        self.account = self.directory / "synthetic account"
        self.account.mkdir(mode=0o700)
        # Do not build or pull an image implicitly in an offline integration test.
        self.docker = accounts.Docker(IMAGE, 30)

    def api(self, operation, *arguments):
        return json.loads(self.docker.run(
            self.account,
            ["opencode", "api", operation, *arguments, "--standalone"],
            operation="Synthetic offline usage test", offline=True,
        ))

    def create_session(self, **body):
        return self.api("session.create", "--data", json.dumps(body))["data"]

    def seed_sessions(self):
        earlier = self.create_session(title="Earlier root", location={"directory": "/tmp"})
        root = self.create_session(title="Current root", location={"directory": "/tmp"})
        child = self.create_session(title="Child", parentID=root["id"])
        sessions = [earlier, root, child]
        with closing(sqlite3.connect(self.account / "opencode.db")) as database, database:
            # These are persisted session counters with no transcript messages.
            # Collection must not infer usage from completed assistant messages.
            for index, session in enumerate(sessions, 1):
                database.execute(
                    "UPDATE session_v2 SET tokens_input=?,tokens_output=?,"
                    "tokens_reasoning=?,tokens_cache_read=?,tokens_cache_write=?,"
                    "cost=0 WHERE id=?",
                    (10 * index, 20 * index, 30 * index, 40 * index, 50 * index, session["id"]),
                )
            database.execute(
                "UPDATE session_v2 SET time_archived=time_created, idle_outcome='failed' WHERE id=?",
                (earlier["id"],),
            )
            self.assertEqual(database.execute("SELECT count(*) FROM session_message").fetchone()[0], 0)
        return sessions

    def test_native_pagination_includes_earlier_archived_root_and_child(self):
        earlier, root, child = self.seed_sessions()
        first = self.api("session.list", "--param", "limit=2", "--param", "order=asc")
        self.assertEqual([item["id"] for item in first["data"]], [earlier["id"], root["id"]])
        self.assertIn("archived", first["data"][0]["time"])
        self.assertEqual(first["data"][0]["outcome"], "failed")
        second = self.api(
            "session.list", "--param", "limit=2", "--param", "cursor=" + first["cursor"]["next"],
        )
        self.assertEqual([item["id"] for item in second["data"]], [child["id"]])
        self.assertEqual(second["data"][0]["parentID"], root["id"])
        self.assertEqual(second["data"][0]["tokens"], {
            "input": 30, "output": 60, "reasoning": 90, "cache": {"read": 120, "write": 150},
        })
        # v2.3.0 supplies a next cursor even on the final nonempty page.
        self.assertIsInstance(second["cursor"]["next"], str)
        final = self.api(
            "session.list", "--param", "limit=2", "--param", "cursor=" + second["cursor"]["next"],
        )
        self.assertEqual(final["data"], [])
        self.assertIsNone(final["cursor"]["next"])

    def test_collector_saves_all_session_counters_without_transcript_messages(self):
        earlier, root, child = self.seed_sessions()
        with patch.object(sys, "path", [str(ROOT), *sys.path]):
            usage = load_module("opencode_usage_integration", ROOT / "src/controller/opencode_usage.py")
        output = self.directory / "result" / "opencode_usage.json"
        with usage.account_tools.account_lock(self.account), patch.object(usage, "PAGE_SIZE", 2):
            snapshot = usage.collect_account_usage(
                self.account, output, image=IMAGE, game_alias="synthetic-1", game="synthetic",
            )
        self.assertEqual(snapshot["status"], "collected")
        self.assertEqual(snapshot["source"], "opencode.session_counters")
        self.assertEqual(snapshot["tokens"], {
            "input": 60, "output": 120, "reasoning": 180, "cache": {"read": 240, "write": 300},
        })
        self.assertEqual(snapshot["totals"], {
            "input_tokens": 600, "cached_input_tokens": 240, "output_tokens": 300,
        })
        sessions = {session["id"]: session for session in snapshot["sessions"]}
        self.assertEqual(set(sessions), {earlier["id"], root["id"], child["id"]})
        self.assertEqual(sessions[child["id"]]["parent_id"], root["id"])
        for session in sessions.values():
            self.assertLessEqual(set(session), {"id", "parent_id", "tokens"})
        self.assertEqual(json.loads(output.read_text()), snapshot)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        # Production analysis must accept the collector's real output even when
        # the agent never wrote a stdout log, and must not depend on transcripts.
        with patch.object(sys, "path", [str(ROOT), *sys.path]):
            from analysis_scripts.extract_raw_cost_tokens import extract_token_counts
        self.assertEqual(extract_token_counts(output.with_name("agent.log")), snapshot["totals"])
        # Exercise the recovery guard against the real Docker state format and
        # save to the original destination when the old run image is unavailable.
        marker = usage.account_tools.pending_usage_path(self.account)
        marker.write_text(json.dumps({
            "schema_version": 1, "account": str(self.account), "game_alias": "synthetic-1", "game": "synthetic",
            "output_path": str(output), "image": "old-run-image-removed",
            "container_name": self.directory.name + "-absent-agent",
        }))
        self.assertEqual(usage.recover_pending_usage(self.account, image=IMAGE), output)
        self.assertFalse(marker.exists())
        self.assertEqual(extract_token_counts(output.with_name("agent.log")), snapshot["totals"])


if __name__ == "__main__":
    unittest.main()
