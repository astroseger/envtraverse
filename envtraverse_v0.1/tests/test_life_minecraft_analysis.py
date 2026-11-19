"""Artifact-only regression tests for the standalone Life Minecraft analyzer."""
from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(os.environ.get(
    "LIFE_MINECRAFT_ANALYSIS_PATH",
    str(Path(__file__).resolve().parents[1] / "analysis_scripts/life_minecraft/analysis_v1.py"),
))
SPEC = importlib.util.spec_from_file_location("life_minecraft_analysis_v1_tested", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
analysis = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = analysis
SPEC.loader.exec_module(analysis)

START = 1_700_000_000.0
SESSION = "a" * 32


class MinecraftAnalysisTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write_json(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")
        return path

    def write_lines(self, relative, values):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")
        return path

    def metadata(self, task="kill_zombie", **extra):
        value = {
            "session_id": SESSION,
            "task_id": task,
            "status": "running",
            "created_at": START - 5,
            "ready_at": START,
            "settings": {"minecraft_task_timeout_seconds": 60},
        }
        value.update(extra)
        return self.write_json(f"run_mc/sessions/{SESSION}/session.json", value)

    def event(self, seq, kind, data=None, seconds=None):
        return {
            "schema_version": 1,
            "run_id": SESSION,
            "session_id": "minecraft-process-1",
            "seq": seq,
            "type": kind,
            "time_ms": int((START + (seq if seconds is None else seconds)) * 1000),
            "data": {} if data is None else data,
        }

    def result(self, success=False, reason="minecraft_task_timeout"):
        return self.write_json(f"run_mc/sessions/{SESSION}/result.json", {
            "session_id": SESSION,
            "task_id": "kill_zombie",
            "status": "completed" if success else "failed",
            "reason": reason,
            "success": success,
            "elapsed_seconds": 60,
            "finished_at": START + 60,
        })

    def game(self):
        games = analysis.analyze_run(self.root)
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["session_id"], SESSION)
        return games[0]

    def test_mirrored_events_and_action_audit_records_are_not_double_counted(self):
        self.metadata()
        self.result(success=True, reason="goal_achieved")
        events = [
            self.event(1, "death", {"name": "AgentPlayer", "direct_attacker": {"name": "Skeleton"}}),
            self.event(2, "respawn", {"after_death": True}),
            self.event(3, "respawn", {"after_death": False}),
            self.event(4, "kill", {"entity_type": "minecraft:zombie", "credited_to_player": True}),
            self.event(5, "entity_death", {"entity_type": "minecraft:zombie", "prime_adversary_is_player": True}),
            self.event(6, "craft", {"item": "minecraft:wooden_sword", "count": 1}),
        ]
        self.write_lines(f"run_mc/sessions/{SESSION}/telemetry/events.jsonl", events)
        self.write_lines(f"run_mc/sessions/{SESSION}/events.jsonl", [dict(event, source="minecraft") for event in events])
        self.write_lines(f"run_mc/sessions/{SESSION}/actions.jsonl", [
            {"action_id": 1, "action": {"id": "key_press", "key": "w"}, "accepted": True, "accepted_at": START + 1},
            {"source": "bridge", "seq": 1, "type": "action_applied", "data": {"action": {"id": "key_press", "key": "w"}}},
            {"source": "bridge", "seq": 2, "type": "automatic_release", "data": {"key": "w"}},
            {"action_id": 2, "action": {"id": "key_press", "key": "w"}, "accepted": False, "terminal": True},
        ])
        game = self.game()
        self.assertEqual(game["outcome"], "completed")
        self.assertEqual((game["deaths"], game["respawns"]), (1, 1))
        self.assertEqual(game["kills"], {"minecraft:zombie": 1})
        self.assertEqual(game["crafted"], {"minecraft:wooden_sword": 1})
        self.assertEqual(game["accepted_actions"], 1)

    def test_kill_after_timeout_does_not_change_failed_outcome(self):
        self.metadata()
        self.result()
        self.write_lines(f"run_mc/sessions/{SESSION}/telemetry/events.jsonl", [
            self.event(1, "join", {"name": "AgentPlayer"}, seconds=0),
            self.event(2, "kill", {"entity_type": "minecraft:zombie", "credited_to_player": True}, seconds=65),
        ])
        game = self.game()
        self.assertEqual(game["outcome"], "failed")
        self.assertEqual(game["reason"], "minecraft_task_timeout")
        self.assertEqual(game["elapsed_seconds"], 60)
        self.assertTrue(game["post_result_events"])

    def test_task_name_falls_back_to_runner_mapping_or_server_log(self):
        for source in ("runner", "server"):
            with self.subTest(source=source):
                metadata = self.metadata()
                value = json.loads(metadata.read_text())
                value.pop("task_id")
                metadata.write_text(json.dumps(value))
                runner = self.root / "run/runner.log"
                server = self.root / f"run/{SESSION}/server/server.log"
                for path in (runner, server):
                    path.unlink(missing_ok=True)
                if source == "runner":
                    runner.parent.mkdir(parents=True, exist_ok=True)
                    runner.write_text(f"INFO arc-runner: game alias mapping={{'{SESSION}': 'kill_three_skeletons'}}\n")
                else:
                    server.parent.mkdir(parents=True, exist_ok=True)
                    server.write_text(f"INFO env-server.server: starting requested_game_id={SESSION} game_id=kill_three_skeletons\n")
                self.assertEqual(self.game()["task_id"], "kill_three_skeletons")

    def test_missing_result_uses_numerically_latest_terminal_observation(self):
        self.metadata()
        base = f"run/{SESSION}/observations"
        self.write_json(f"{base}/step_9999_action.json", {"accepted": True, "action_id": 9999})
        self.write_json(f"{base}/step_10000_action.json", {
            "accepted": False, "terminal": True, "status": "failed",
            "reason": "minecraft_task_timeout", "success": False,
        })
        game = self.game()
        self.assertEqual(game["outcome"], "failed")
        self.assertEqual(game["reason"], "minecraft_task_timeout")

    def test_missing_gameplay_data_is_unknown_not_zero_or_success(self):
        self.metadata(status="closed")
        runner = self.root / "run/runner.log"
        runner.parent.mkdir(parents=True, exist_ok=True)
        runner.write_text("INFO arc-runner: runner finished exit_code=0\n")
        game = self.game()
        self.assertEqual(game["outcome"], "unknown")
        for name in ("deaths", "respawns", "kills", "crafted", "accepted_actions"):
            self.assertIsNone(game[name], name)

    def test_partial_jsonl_tail_preserves_valid_events_and_warns(self):
        self.metadata()
        path = self.write_lines(f"run_mc/sessions/{SESSION}/telemetry/events.jsonl", [
            self.event(1, "death", {"name": "AgentPlayer", "direct_attacker": {"name": "Spider"}}),
        ])
        with path.open("a") as output:
            output.write('{"type":"respawn"')
        game = self.game()
        self.assertEqual(game["deaths"], 1)
        self.assertTrue(game["warnings"])

    def test_partial_snapshot_does_not_turn_unrecorded_metrics_into_zero(self):
        self.metadata(status="closed")
        self.write_json(f"run_mc/sessions/{SESSION}/telemetry/state.json", {
            "time_ms": int((START + 60) * 1000),
            "last_event_seq": 30,
            "counters": {"deaths": 7},
        })
        game = self.game()
        self.assertEqual(game["deaths"], 7)
        for name in ("respawns", "kills", "crafted"):
            self.assertIsNone(game[name], name)

    def test_escape_death_is_classified_as_drowning_not_direct_skeleton_kill(self):
        self.metadata()
        self.write_lines(f"run_mc/sessions/{SESSION}/telemetry/events.jsonl", [
            self.event(1, "death", {
                "name": "AgentPlayer", "entity_type": "minecraft:player",
                "credited_entity": {"name": "Skeleton"}, "direct_attacker": None,
            }),
        ])
        # START + 1 is 22:13:21 UTC; Minecraft container death messages use UTC.
        log = self.root / f"run_mc/sessions/{SESSION}/containers.log"
        log.write_text("server-1  | [22:13:21] [Server thread/INFO]: AgentPlayer drowned while trying to escape Skeleton\n")
        game = self.game()
        normalized = {str(key).casefold(): value for key, value in game["death_causes"].items()}
        self.assertEqual(game["deaths"], 1)
        self.assertEqual(normalized.get("drowning"), 1)
        self.assertNotIn("skeleton", normalized)

    def test_json_cli_contains_per_game_outcome(self):
        self.metadata()
        self.result(success=True, reason="goal_achieved")
        output = io.StringIO()
        with redirect_stdout(output):
            code = analysis.main([str(self.root), "--json"])
        self.assertEqual(code, 0)
        value = json.loads(output.getvalue())
        self.assertEqual(len(value["games"]), 1)
        self.assertEqual(value["games"][0]["task_id"], "kill_zombie")
        self.assertEqual(value["games"][0]["outcome"], "completed")

    def test_analysis_reads_only_output_directories_and_never_writes(self):
        self.metadata()
        self.result(success=True, reason="goal_achieved")
        real_open = io.open

        def guarded_open(file, mode="r", *args, **kwargs):
            path = Path(file).resolve()
            self.assertTrue(any(path.is_relative_to(self.root / folder) for folder in ("run", "run_mc")), str(path))
            self.assertNotIn("w", mode)
            self.assertNotIn("a", mode)
            self.assertNotIn("+", mode)
            return real_open(file, mode, *args, **kwargs)

        with patch("io.open", side_effect=guarded_open):
            self.assertEqual(self.game()["outcome"], "completed")


if __name__ == "__main__":
    unittest.main()
