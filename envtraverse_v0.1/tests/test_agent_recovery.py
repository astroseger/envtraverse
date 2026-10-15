from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch

AGENT_SRC = Path(__file__).resolve().parents[1] / "src" / "agents" / "agent_v1" / "agent"
sys.path.insert(0, str(AGENT_SRC))

import agent
import agent_runner
import session_inspector


class AgentRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = self.root / "agent_run"
        self.workspace.mkdir()
        self.helper = self.workspace / "helper.py"
        self.helper.write_text("# keep the existing workspace\n", encoding="utf-8")
        self.state = self.root / ".client_session.json"
        self.description = self.root / "environment_description.md"
        self.description.write_text("Accomplish the environment goal.", encoding="utf-8")
        self.enterContext(patch.multiple(
            agent,
            CLIENT_STATE_PATH=self.state,
            ENVIRONMENT_DESCRIPTION_PATH=self.description,
            LOG_FILE=self.root / "agent.log",
            THREAD_ID_FILE=self.root / ".codex_thread_id",
            THREAD_ID_TEMP_FILE=self.root / ".codex_thread_id.tmp",
        ))
        self.enterContext(patch.object(session_inspector, "CLIENT_STATE_PATH", self.state))
        self.logger = Mock()
        self.enterContext(patch.object(agent, "setup_logger", return_value=self.logger))
        self.subject = agent.SimpleAgent(self.workspace, game_name="example")
        self.status_output = "step: 0\npath: /observations\nfiles:\n  step_0000_state.txt\n"
        self.client = self.enterContext(patch.object(self.subject, "run_client", return_value=self.status_output))
        self.new_session = self.enterContext(patch.object(
            self.subject.runner, "new_session", wraps=self.subject.runner.new_session,
        ))
        self.resume_session = self.enterContext(patch.object(
            self.subject.runner, "resume_session", wraps=self.subject.runner.resume_session,
        ))
        self.send = self.enterContext(patch.object(
            self.subject.runner, "send", side_effect=self.report_new_thread,
        ))
        self.thread_count = 0
        self.write_state(0)

    def write_state(self, step: int, terminal: bool = False) -> None:
        self.state.write_text(json.dumps({"step_index": step, "terminal": terminal}), encoding="utf-8")

    def report_new_thread(self, prompt: str) -> None:
        # Model the CLI's thread.started callback without invoking Codex.
        if self.subject.runner.current_thread_id is None:
            self.thread_count += 1
            self.subject.runner._capture_thread_id({"thread_id": f"new-thread-{self.thread_count}"})

    def expected_initial_prompt(self) -> str:
        return (
            self.subject.main_prompt + "\n\n" + self.subject.continuation_string
            + "\n\nThe initial output of the game client:\n" + self.status_output
        )

    def test_step_zero_uses_status_and_a_new_thread_without_an_old_thread_file(self) -> None:
        self.assertFalse(agent.THREAD_ID_FILE.exists())
        self.subject.init_recovery()
        self.client.assert_called_once_with("status")
        self.new_session.assert_called_once_with()
        self.resume_session.assert_not_called()
        self.send.assert_called_once_with(self.expected_initial_prompt())
        self.assertEqual(agent.load_thread_id(), "new-thread-1")
        self.assertEqual(self.helper.read_text(encoding="utf-8"), "# keep the existing workspace\n")
        self.logger.info.assert_any_call(
            "start init_recovery",
            extra={"step_index": 0, "terminal": False, "client_status": self.status_output},
        )

    def test_master_and_step_zero_recovery_share_the_same_initial_prompt(self) -> None:
        self.subject.init_iterations()
        self.subject.init_recovery()
        self.assertEqual(self.client.call_args_list, [call("start example"), call("status")])
        self.assertEqual(self.send.call_args_list, [call(self.expected_initial_prompt())] * 2)
        self.assertEqual(self.new_session.call_count, 2)
        self.resume_session.assert_not_called()
        self.assertEqual(agent.load_thread_id(), "new-thread-2")

    def test_step_zero_replaces_the_old_id_and_later_recovery_resumes_the_new_id(self) -> None:
        agent.save_thread_id("old-thread")
        self.subject.init_recovery()
        self.assertEqual(agent.load_thread_id(), "new-thread-1")
        self.write_state(1)
        self.client.return_value = "step: 1\n"
        self.subject.init_recovery()
        self.new_session.assert_called_once_with()
        self.resume_session.assert_called_once_with("new-thread-1")
        self.assertEqual(self.send.call_args_list[-1], call(self.subject.continuation_prompt()))
        self.assertEqual(agent.load_thread_id(), "new-thread-1")

    def test_status_repair_of_pending_first_move_selects_resume_not_new_thread(self) -> None:
        agent.save_thread_id("old-thread")

        def repair_status(command: str) -> str:
            self.assertEqual(command, "status")
            self.write_state(1)
            return "Recovered the last server step.\nstep: 1\n"

        self.client.side_effect = repair_status
        self.subject.init_recovery()
        self.new_session.assert_not_called()
        self.resume_session.assert_called_once_with("old-thread")
        self.send.assert_called_once_with(self.subject.continuation_prompt())

    def test_terminal_status_stops_without_starting_or_resuming_codex(self) -> None:
        def terminal_status(command: str) -> str:
            self.write_state(0, terminal=True)
            return "step: 0\n"

        self.client.side_effect = terminal_status
        self.assertEqual(self.subject.run(recovery=True), 0)
        self.client.assert_called_once_with("status")
        self.new_session.assert_not_called()
        self.resume_session.assert_not_called()
        self.send.assert_not_called()

    def test_failed_status_does_not_start_codex(self) -> None:
        self.client.side_effect = subprocess.CalledProcessError(1, ["client.py", "status"])
        self.assertEqual(self.subject.run(recovery=True), 1)
        self.new_session.assert_not_called()
        self.resume_session.assert_not_called()
        self.send.assert_not_called()
        self.assertFalse(agent.THREAD_ID_FILE.exists())

    def test_invalid_description_prevents_step_zero_restart(self) -> None:
        self.description.write_text(" \n", encoding="utf-8")
        self.assertEqual(self.subject.run(recovery=True), 1)
        self.client.assert_called_once_with("status")
        self.new_session.assert_not_called()
        self.send.assert_not_called()

    def test_progressed_game_still_requires_a_saved_codex_thread_id(self) -> None:
        self.write_state(2)
        self.assertFalse(agent.THREAD_ID_FILE.exists())
        self.assertEqual(self.subject.run(recovery=True), 1)
        self.new_session.assert_not_called()
        self.resume_session.assert_not_called()
        self.send.assert_not_called()


class AgentSupervisorRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.root / ".client_session.json"
        self.log_file = self.root / "agent.log"
        self.enterContext(patch.multiple(
            agent_runner,
            CLIENT_STATE_PATH=self.state,
            RUNNER_LOG_FILE=self.root / "agent_runner.log",
            LOG_FILE=self.log_file,
        ))
        self.enterContext(patch.object(session_inspector, "CLIENT_STATE_PATH", self.state))
        self.log = self.enterContext(patch.object(agent_runner, "log"))
        self.process = Mock(returncode=17)
        self.start = self.enterContext(patch.object(agent_runner, "start_agent", return_value=self.process))
        self.real_wait = agent_runner.wait_for_agent
        self.wait = self.enterContext(patch.object(agent_runner, "wait_for_agent", side_effect=["exited", "stop"]))

    def write_state(self, step: int = 0, terminal: bool = False) -> None:
        self.state.write_text(json.dumps({"step_index": step, "terminal": terminal}), encoding="utf-8")

    def test_started_game_at_step_zero_enters_recovery(self) -> None:
        self.write_state()
        self.assertEqual(agent_runner.main(["example", "--model", "test-model", "--reasoning-effort", "max"]), 0)
        self.assertEqual(self.start.call_count, 2)
        master_command = self.start.call_args_list[0].args[0]
        recovery_command = self.start.call_args_list[1].args[0]
        self.assertIn("--master", master_command)
        self.assertIn("--recovery", recovery_command)
        self.assertNotIn("--master", recovery_command)
        self.assertEqual(recovery_command[recovery_command.index("--model") + 1], "test-model")

    def test_missing_client_session_remains_an_unrecoverable_startup_failure(self) -> None:
        self.assertEqual(agent_runner.main(["example"]), 17)
        self.assertEqual(self.start.call_count, 1)
        self.log.assert_any_call(
            "agent_runner.py: master stopped without a saved client session; not entering recovery"
        )

    def test_terminal_session_does_not_start_recovery(self) -> None:
        self.write_state(terminal=True)
        self.assertEqual(agent_runner.main(["example"]), 0)
        self.assertEqual(self.start.call_count, 1)

    def test_step_zero_recoveries_are_bounded_by_the_existing_attempt_limit(self) -> None:
        self.write_state()
        self.wait.side_effect = None
        self.wait.return_value = "exited"
        self.assertEqual(agent_runner.main(["example"]), 0)
        self.assertEqual(self.start.call_count, 1 + agent_runner.MAX_RECOVERY_ATTEMPTS)
        self.log.assert_any_call("agent_runner.py: recovery attempt limit reached (10); stopping supervisor")

    def test_silent_initialization_can_wait_over_an_hour_before_two_hour_recovery(self) -> None:
        self.process.poll.return_value = None
        # No client session or agent.log yet: simulate slow initial setup.
        with (
            patch.object(agent_runner.time, "time", side_effect=[0, 1800, 3600, 3900, 7199, 7200]),
            patch.object(agent_runner.time, "sleep") as sleep,
            patch.object(agent_runner, "inspect_and_should_stop", return_value=False) as inspect,
            patch.object(agent_runner, "terminate_agent") as terminate,
        ):
            self.assertEqual(self.real_wait(self.process, "master"), "stale")
        self.assertEqual(sleep.call_count, 4)
        inspect.assert_called_once_with()
        terminate.assert_called_once_with(self.process)
        self.log.assert_any_call("agent_runner.py: agent.log is stale for 7200 seconds; stopping master")

    def test_watchdog_ignores_fresh_stderr_log(self) -> None:
        self.log_file.write_text("old event\n", encoding="utf-8")
        os.utime(self.log_file, (100, 100))
        stderr_log = self.root / "codex_stderr.log"
        stderr_log.write_text("still retrying\n", encoding="utf-8")
        os.utime(stderr_log, (2000, 2000))
        with patch.object(agent_runner.time, "time", return_value=2001):
            self.assertEqual(agent_runner.log_idle_seconds(99), 1901)


if __name__ == "__main__":
    unittest.main()
