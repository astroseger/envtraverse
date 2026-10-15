from __future__ import annotations

import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch


AGENT_SRC = (
    Path(__file__).resolve().parents[1]
    / "src/agents/test_arc3_agent_v1_02_wstop_wreset/agent"
)

# This variant uses the same module names as agent_v1. Keep its imports isolated
# so discovering these tests cannot change which agent the other tests exercise.
with patch.dict(sys.modules), patch.object(sys, "path", [str(AGENT_SRC), *sys.path]):
    for name in ("agent", "agent_funs", "codex_runner", "session_inspector"):
        sys.modules.pop(name, None)
    agent = importlib.import_module("agent")
    inspector = importlib.import_module("session_inspector")


RESET_COMMAND = 'move \'{"id":"RESET"}\''


class Arc3AgentResetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = self.root / "agent_run"
        self.workspace.mkdir()
        self.state = self.root / ".client_session.json"
        self.observations = self.root / "observations"
        self.observations.mkdir()
        self.description = self.observations / "environment_description.md"
        self.description.write_text("Solve the ARC3 game.", encoding="utf-8")
        self.enterContext(patch.multiple(
            inspector, CLIENT_STATE_PATH=self.state, OBSERVATIONS_DIR=self.observations,
        ))
        self.enterContext(patch.multiple(
            agent,
            LOG_FILE=self.root / "agent.log",
            THREAD_ID_FILE=self.root / ".codex_thread_id",
            THREAD_ID_TEMP_FILE=self.root / ".codex_thread_id.tmp",
            ENVIRONMENT_DESCRIPTION_PATH=self.description,
        ))
        self.logger = Mock()
        self.enterContext(patch.object(agent, "setup_logger", return_value=self.logger))
        self.subject = agent.SimpleAgent(self.workspace, game_name="example")
        self.send = self.enterContext(patch.object(self.subject.runner, "send"))
        self.client = self.enterContext(patch.object(self.subject, "run_client", return_value="step: 127\n"))
        self.snapshot = self.enterContext(patch.object(self.subject, "run_git"))
        self.enterContext(patch.object(agent.time, "sleep"))

    def write_step(self, step: int, game_state: str, *, terminal: bool = False) -> Path:
        metadata = self.observations / f"step_{step:04d}_metadata.txt"
        metadata.write_text(json.dumps({"state": game_state}), encoding="utf-8")
        self.state.write_text(json.dumps({"step_index": step, "terminal": terminal}), encoding="utf-8")
        return metadata

    def test_no_session_needs_no_metadata(self) -> None:
        self.assertEqual(inspector.inspect_sessions().to_dict(), {
            "step_index": 0, "terminal": False, "is_game_over": False,
        })

    def test_inspection_distinguishes_game_over_from_terminal(self) -> None:
        for state, terminal in (
            ("NOT_STARTED", False), ("NOT_FINISHED", False),
            ("GAME_OVER", False), ("GAME_OVER", True), ("WIN", True),
        ):
            with self.subTest(state=state, terminal=terminal):
                self.write_step(126, state, terminal=terminal)
                self.assertEqual(inspector.inspect_sessions().to_dict(), {
                    "step_index": 126, "terminal": terminal, "is_game_over": state == "GAME_OVER",
                })

    def test_inspection_uses_the_session_step_not_other_metadata(self) -> None:
        self.write_step(125, "GAME_OVER")
        self.write_step(127, "GAME_OVER")
        self.write_step(126, "NOT_FINISHED")
        self.assertFalse(inspector.inspect_sessions().is_game_over)

    def test_missing_metadata_raises(self) -> None:
        self.write_step(126, "GAME_OVER").unlink()
        with self.assertRaises(FileNotFoundError):
            inspector.inspect_sessions()

    def test_invalid_metadata_raises(self) -> None:
        for content, error in (("broken JSON", ValueError), ("{}", KeyError), ("[]", TypeError)):
            with self.subTest(content=content):
                self.write_step(126, "GAME_OVER").write_text(content, encoding="utf-8")
                with self.assertRaises(error):
                    inspector.inspect_sessions()

    def test_game_over_resets_before_stuck_and_continues_the_same_thread(self) -> None:
        self.write_step(126, "GAME_OVER")
        self.subject.runner.resume_session("existing-thread")
        self.subject.previous_loop_step_index = 126

        def reset(command: str) -> str:
            self.assertEqual(command, RESET_COMMAND)
            self.write_step(127, "NOT_FINISHED")
            return "step: 127\nfiles: /observations/step_0127_metadata.txt\n"

        def solve_after_reset(prompt: str) -> None:
            self.assertFalse(inspector.inspect_sessions().is_game_over)
            self.assertEqual(self.subject.runner.current_thread_id, "existing-thread")
            self.assertIn("The level has been reset. You have another attempt.", prompt)
            self.assertIn("/observations/step_0127_metadata.txt", prompt)
            self.write_step(128, "WIN", terminal=True)

        self.client.side_effect = reset
        self.send.side_effect = solve_after_reset
        with patch.object(self.subject, "init_iterations"):
            self.assertEqual(self.subject.run(), 0)
        self.client.assert_called_once_with(RESET_COMMAND)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(self.snapshot.call_count, 1)
        self.assertIsNone(self.subject.last_stuck_step)

    def test_running_game_keeps_normal_and_stuck_protocols(self) -> None:
        self.write_step(126, "NOT_FINISHED")
        inspection = inspector.inspect_sessions()
        self.subject.iteration_loop(inspection)
        self.subject.iteration_loop(inspection)
        self.client.assert_not_called()
        self.assertEqual(self.send.call_args_list, [
            call(self.subject.continuation_prompt()),
            call(self.subject.continuation_prompt() + "\n" + self.subject.stuck_reminder_prompt),
        ])
        self.assertTrue(self.subject.stop_condition(inspection))

    def test_terminal_game_over_stops_without_resetting(self) -> None:
        self.write_step(126, "GAME_OVER", terminal=True)
        with patch.object(self.subject, "init_iterations"):
            self.assertEqual(self.subject.run(), 0)
        self.client.assert_not_called()
        self.send.assert_not_called()
        self.snapshot.assert_not_called()

    def test_failed_reset_aborts_without_claiming_another_attempt(self) -> None:
        self.write_step(126, "GAME_OVER")
        self.client.side_effect = subprocess.CalledProcessError(1, ["client.py", "move"])
        with patch.object(self.subject, "init_iterations"):
            self.assertEqual(self.subject.run(), 1)
        self.client.assert_called_once_with(RESET_COMMAND)
        self.send.assert_not_called()

    def test_recovery_checks_repaired_status_then_resets_in_resumed_thread(self) -> None:
        self.write_step(125, "NOT_FINISHED")
        agent.save_thread_id("saved-thread")

        def client(command: str) -> str:
            if command == "status":
                self.write_step(126, "GAME_OVER")
                return "recovered step: 126\n"
            self.assertEqual(command, RESET_COMMAND)
            self.assertEqual(self.subject.runner.current_thread_id, "saved-thread")
            self.write_step(127, "NOT_FINISHED")
            return "reset step: 127\n"

        self.client.side_effect = client
        self.subject.init_recovery()
        self.assertEqual(self.client.call_args_list, [call("status"), call(RESET_COMMAND)])
        self.assertEqual(self.subject.runner.current_thread_id, "saved-thread")
        self.assertEqual(self.send.call_count, 1)
        self.assertIn("The level has been reset.", self.send.call_args.args[0])
        self.assertIn("reset step: 127", self.send.call_args.args[0])

    def test_recovery_of_running_game_continues_without_reset(self) -> None:
        self.write_step(126, "NOT_FINISHED")
        agent.save_thread_id("saved-thread")
        self.subject.init_recovery()
        self.client.assert_called_once_with("status")
        self.assertEqual(self.subject.runner.current_thread_id, "saved-thread")
        self.send.assert_called_once_with(self.subject.continuation_prompt())

    def test_step_zero_recovery_still_starts_a_new_thread(self) -> None:
        self.write_step(0, "NOT_STARTED")
        self.subject.runner.resume_session("old-thread")
        self.subject.init_recovery()
        self.client.assert_called_once_with("status")
        self.assertIsNone(self.subject.runner.current_thread_id)
        self.assertEqual(self.send.call_count, 1)
        self.assertIn(self.subject.main_prompt, self.send.call_args.args[0])

    def test_terminal_recovery_stops_without_a_thread_or_reset(self) -> None:
        self.write_step(126, "GAME_OVER", terminal=True)
        self.assertEqual(self.subject.run(recovery=True), 0)
        self.client.assert_called_once_with("status")
        self.send.assert_not_called()

    def test_bad_metadata_aborts_both_iteration_and_recovery(self) -> None:
        self.write_step(126, "GAME_OVER").write_text("{}", encoding="utf-8")
        with patch.object(self.subject, "init_iterations"):
            self.assertEqual(self.subject.run(), 1)
        self.client.assert_not_called()
        self.assertEqual(self.subject.run(recovery=True), 1)
        self.client.assert_called_once_with("status")
        self.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
