from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch


ROOT = Path(__file__).resolve().parents[1]


def load_agent_modules(agent_name: str):
    agent_src = ROOT / "src/agents" / agent_name / "agent"
    # Agent variants share module names; restore the import cache and path so
    # each test exercises its selected variant without affecting other tests.
    with patch.dict(sys.modules), patch.object(sys, "path", [str(agent_src), str(ROOT), *sys.path]):
        for name in ("agent", "agent_funs", "agent_runner", "codex_runner", "session_inspector"):
            sys.modules.pop(name, None)
        runner = importlib.import_module("agent_runner")
        inspector = importlib.import_module("session_inspector")
        prepare_run = importlib.import_module(f"src.agents.{agent_name}.prepare_run").prepare_run
    return runner, inspector, prepare_run


class AgentV1RecoveryDelayPreparationTest(unittest.TestCase):
    agent_name = "agent_v1"

    def setUp(self) -> None:
        self.runner, _, self.prepare_run = load_agent_modules(self.agent_name)

    def test_omitted_or_zero_delay_preserves_the_launch_command(self) -> None:
        for settings in ({}, {"recovery_delay_seconds": 0}, {"recovery_delay_seconds": 0.0}):
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as directory:
                preparation = self.prepare_run(
                    Path(directory), "http://server:8879", "example",
                    {"model": "test-model", "reasoning_effort": "medium", **settings},
                )
                self.assertEqual(preparation.command, [
                    "python3", "agent_runner.py", "example",
                    "--model", "test-model", "--reasoning-effort", "medium",
                ])
                self.assertEqual(self.runner.parse_args(preparation.command[2:]).recovery_delay_seconds, 0)
                self.assertEqual(self.runner.parse_args(preparation.command[2:]).supervisor_inactivity_timeout_seconds, 7200)
                self.assertEqual(json.loads((Path(directory) / "client/client_config.json").read_text())[
                    "http_read_timeout_seconds"], 3900)

    def test_integer_and_fractional_delays_reach_the_runner(self) -> None:
        for delay in (60, 0.25):
            with self.subTest(delay=delay), tempfile.TemporaryDirectory() as directory:
                preparation = self.prepare_run(
                    Path(directory), "http://server:8879", "example",
                    {"model": "test-model", "reasoning_effort": "medium", "recovery_delay_seconds": delay},
                )
                args = self.runner.parse_args(preparation.command[2:])
                self.assertEqual(args.recovery_delay_seconds, delay)
                self.assertEqual(args.model, "test-model")
                self.assertEqual(args.reasoning_effort, "medium")

    def test_invalid_delays_fail_before_creating_run_files(self) -> None:
        for delay in (
            None, True, False, "60", [], {}, -1, -0.5,
            float("nan"), float("inf"), float("-inf"), 10**400,
        ):
            with self.subTest(delay=delay), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(RuntimeError, "recovery_delay_seconds"):
                    self.prepare_run(
                        Path(directory), "http://server:8879", "example",
                        {"model": "test-model", "reasoning_effort": "medium", "recovery_delay_seconds": delay},
                    )
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_recovery_limits_reach_the_runner_without_changing_default_commands(self) -> None:
        for limit in (0, 2, 10, 20):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as directory:
                preparation = self.prepare_run(
                    Path(directory), "http://server:8879", "example",
                    {"model": "test-model", "reasoning_effort": "medium", "max_recovery_attempts": limit},
                )
                self.assertEqual(self.runner.parse_args(preparation.command[2:]).max_recovery_attempts, limit)
                self.assertEqual("--max-recovery-attempts" in preparation.command, limit != 10)

    def test_invalid_recovery_limits_fail_before_creating_run_files(self) -> None:
        for limit in (None, True, False, "2", [], {}, -1, 1.5, float("nan"), float("inf")):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(RuntimeError, "max_recovery_attempts"):
                    self.prepare_run(
                        Path(directory), "http://server:8879", "example",
                        {"model": "test-model", "reasoning_effort": "medium", "max_recovery_attempts": limit},
                    )
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_timeouts_reach_the_supervisor_and_client(self) -> None:
        for http_timeout, inactivity_timeout in ((36300, 39600), (0.25, 0.5)):
            with self.subTest(http=http_timeout, inactivity=inactivity_timeout), tempfile.TemporaryDirectory() as directory:
                preparation = self.prepare_run(
                    Path(directory), "http://server:8879", "example",
                    {"model": "test-model", "reasoning_effort": "medium",
                     "http_read_timeout_seconds": http_timeout,
                     "supervisor_inactivity_timeout_seconds": inactivity_timeout},
                )
                args = self.runner.parse_args(preparation.command[2:])
                self.assertEqual(args.supervisor_inactivity_timeout_seconds, inactivity_timeout)
                saved = json.loads((Path(directory) / "client/client_config.json").read_text())
                self.assertEqual(saved["http_read_timeout_seconds"], http_timeout)
                self.assertNotIn("supervisor_inactivity_timeout_seconds", saved)

    def test_invalid_timeouts_fail_before_creating_run_files(self) -> None:
        for key in ("http_read_timeout_seconds", "supervisor_inactivity_timeout_seconds"):
            for value in (None, True, False, "60", [], {}, 0, -1, float("nan"), float("inf"), 10**400):
                with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as directory:
                    with self.assertRaisesRegex(RuntimeError, key):
                        self.prepare_run(
                            Path(directory), "http://server:8879", "example",
                            {"model": "test-model", "reasoning_effort": "medium", key: value},
                        )
                    self.assertEqual(list(Path(directory).iterdir()), [])


class WstopRecoveryDelayPreparationTest(AgentV1RecoveryDelayPreparationTest):
    agent_name = "agent_v1_01_wstop"


class Wstop2RecoveryDelayPreparationTest(AgentV1RecoveryDelayPreparationTest):
    agent_name = "agent_v1_07_wstop2"


class WresetRecoveryDelayPreparationTest(AgentV1RecoveryDelayPreparationTest):
    agent_name = "test_arc3_agent_v1_02_wstop_wreset"


class AgentV1RecoveryDelaySupervisorTest(unittest.TestCase):
    agent_name = "agent_v1"

    def setUp(self) -> None:
        runner, inspector, _ = load_agent_modules(self.agent_name)
        self.runner = runner
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.root / ".client_session.json"
        self.write_state()
        self.enterContext(patch.multiple(
            runner,
            CLIENT_STATE_PATH=self.state,
            RUNNER_LOG_FILE=self.root / "agent_runner.log",
            LOG_FILE=self.root / "agent.log",
        ))
        self.enterContext(patch.multiple(inspector, CLIENT_STATE_PATH=self.state, OBSERVATIONS_DIR=self.root))
        self.log = self.enterContext(patch.object(runner, "log"))
        self.process = Mock(returncode=17)
        self.start = self.enterContext(patch.object(runner, "start_agent", return_value=self.process))
        self.real_wait = runner.wait_for_agent
        self.wait = self.enterContext(patch.object(runner, "wait_for_agent", return_value="exited"))
        self.sleep = self.enterContext(patch.object(runner.time, "sleep"))

    def write_state(self, *, terminal: bool = False) -> None:
        self.state.write_text(json.dumps({"step_index": 0, "terminal": terminal}), encoding="utf-8")
        (self.root / "step_0000_metadata.txt").write_text(
            json.dumps({"state": "WIN" if terminal else "NOT_FINISHED"}), encoding="utf-8",
        )

    def test_omitted_and_zero_delay_recover_without_sleep_or_extra_inspections(self) -> None:
        for arguments in ([], ["--recovery-delay-seconds", "0"]):
            with self.subTest(arguments=arguments), patch.object(
                self.runner, "inspect_and_should_stop", wraps=self.runner.inspect_and_should_stop,
            ) as inspect:
                self.start.reset_mock()
                self.log.reset_mock()
                self.assertEqual(self.runner.main(["example", *arguments]), 0)
                self.sleep.assert_not_called()
                self.assertEqual(self.start.call_count, 1 + self.runner.MAX_RECOVERY_ATTEMPTS)
                self.assertEqual(inspect.call_count, 1 + self.runner.MAX_RECOVERY_ATTEMPTS)
                self.assertFalse(any("seconds before recovery" in entry.args[0] for entry in self.log.call_args_list))

    def test_delay_precedes_every_recovery_after_exits_and_stale_processes(self) -> None:
        events = Mock()
        events.attach_mock(self.start, "start")
        events.attach_mock(self.wait, "wait")
        events.attach_mock(self.sleep, "sleep")
        self.wait.side_effect = ["exited", "stale", "stop"]

        self.assertEqual(self.runner.main(["example", "--recovery-delay-seconds", "0.25"]), 0)

        master_command = self.start.call_args_list[0].args[0]
        recovery_command = self.start.call_args_list[1].args[0]
        self.assertIn("--master", master_command)
        self.assertIn("--recovery", recovery_command)
        self.assertNotIn("--recovery-delay-seconds", recovery_command)
        self.assertEqual(events.mock_calls, [
            call.start(master_command),
            call.wait(self.process, "master", inactivity_timeout_seconds=7200),
            call.sleep(0.25),
            call.start(recovery_command),
            call.wait(self.process, "recovery", inactivity_timeout_seconds=7200),
            call.sleep(0.25),
            call.start(recovery_command),
            call.wait(self.process, "recovery", inactivity_timeout_seconds=7200),
        ])
        self.log.assert_any_call("agent_runner.py: waiting 0.25 seconds before recovery")

    def test_delay_also_applies_when_the_master_becomes_stale(self) -> None:
        self.wait.side_effect = ["stale", "stop"]
        self.assertEqual(self.runner.main(["example", "--recovery-delay-seconds", "60"]), 0)
        self.sleep.assert_called_once_with(60)
        self.assertEqual(self.start.call_count, 2)

    def test_configured_inactivity_limit_applies_to_master_and_every_recovery(self) -> None:
        self.wait.side_effect = ["stale", "exited", "stop"]
        self.assertEqual(self.runner.main(["example", "--supervisor-inactivity-timeout-seconds", "39600"]), 0)
        self.assertEqual(self.wait.call_args_list, [
            call(self.process, "master", inactivity_timeout_seconds=39600),
            call(self.process, "recovery", inactivity_timeout_seconds=39600),
            call(self.process, "recovery", inactivity_timeout_seconds=39600),
        ])

    def test_custom_inactivity_limit_allows_silence_past_the_old_default(self) -> None:
        self.process.poll.return_value = None
        with (
            patch.object(self.runner.time, "time", side_effect=[0, 7200, 36300, 39599, 39600]),
            patch.object(self.runner, "inspect_and_should_stop", return_value=False) as inspect,
            patch.object(self.runner, "terminate_agent") as terminate,
        ):
            self.assertEqual(self.real_wait(self.process, "master", inactivity_timeout_seconds=39600), "stale")
        self.assertEqual(self.sleep.call_count, 3)
        inspect.assert_called_once_with()
        terminate.assert_called_once_with(self.process)

    def test_agent_log_activity_restarts_the_configured_inactivity_window(self) -> None:
        self.process.poll.return_value = None
        self.runner.LOG_FILE.write_text("initial event\n")
        os.utime(self.runner.LOG_FILE, (0, 0))
        updates = iter((9, 18))

        def update_log(_seconds):
            timestamp = next(updates)
            os.utime(self.runner.LOG_FILE, (timestamp, timestamp))

        self.sleep.side_effect = update_log
        with (
            patch.object(self.runner.time, "time", side_effect=[0, 9, 18, 28]),
            patch.object(self.runner, "inspect_and_should_stop", return_value=False),
            patch.object(self.runner, "terminate_agent") as terminate,
        ):
            self.assertEqual(self.real_wait(self.process, "recovery", inactivity_timeout_seconds=10), "stale")
        self.assertEqual(self.sleep.call_count, 2)
        terminate.assert_called_once_with(self.process)

    def test_invalid_cli_inactivity_limits_fail_before_starting_the_agent(self) -> None:
        for value in ("0", "-1", "nan", "inf", "-inf", "1e400", "not-a-number"):
            with self.subTest(value=value), patch("sys.stderr"), self.assertRaises(SystemExit) as error:
                self.runner.main(["example", f"--supervisor-inactivity-timeout-seconds={value}"])
            self.assertEqual(error.exception.code, 2)
        self.start.assert_not_called()

    def test_no_extra_delay_after_the_recovery_limit(self) -> None:
        self.assertEqual(self.runner.main(["example", "--recovery-delay-seconds", "60"]), 0)
        self.assertEqual(self.sleep.call_args_list, [call(60)] * self.runner.MAX_RECOVERY_ATTEMPTS)
        self.assertEqual(self.start.call_count, 1 + self.runner.MAX_RECOVERY_ATTEMPTS)

    def test_configured_limit_bounds_restarts_and_zero_disables_recovery(self) -> None:
        for limit in (0, 2, 20):
            with self.subTest(limit=limit):
                self.start.reset_mock()
                self.sleep.reset_mock()
                self.assertEqual(self.runner.main([
                    "example", "--max-recovery-attempts", str(limit), "--recovery-delay-seconds", "0.25",
                ]), 0)
                self.assertEqual(self.start.call_count, 1 + limit)
                self.assertEqual(self.sleep.call_args_list, [call(0.25)] * limit)

    def test_terminal_session_skips_delay_and_recovery(self) -> None:
        self.write_state(terminal=True)
        self.assertEqual(self.runner.main(["example", "--recovery-delay-seconds", "60"]), 0)
        self.sleep.assert_not_called()
        self.assertEqual(self.start.call_count, 1)

    def test_master_stop_skips_delay_and_recovery(self) -> None:
        self.wait.return_value = "stop"
        self.assertEqual(self.runner.main(["example", "--recovery-delay-seconds", "60"]), 0)
        self.sleep.assert_not_called()
        self.assertEqual(self.start.call_count, 1)

    def test_missing_session_skips_delay_and_recovery(self) -> None:
        self.state.unlink()
        self.assertEqual(self.runner.main(["example", "--recovery-delay-seconds", "60"]), 17)
        self.sleep.assert_not_called()
        self.assertEqual(self.start.call_count, 1)

    def test_terminal_state_during_delay_prevents_a_recovery_attempt(self) -> None:
        self.sleep.side_effect = lambda seconds: self.write_state(terminal=True)
        self.assertEqual(self.runner.main(["example", "--recovery-delay-seconds", "60"]), 0)
        self.sleep.assert_called_once_with(60)
        self.assertEqual(self.start.call_count, 1)
        self.assertFalse(any("recovery attempt " in entry.args[0] for entry in self.log.call_args_list))

    def test_interrupt_during_delay_does_not_start_or_count_recovery(self) -> None:
        self.sleep.side_effect = KeyboardInterrupt
        self.assertEqual(self.runner.main(["example", "--recovery-delay-seconds", "60"]), 130)
        self.assertEqual(self.start.call_count, 1)
        self.assertFalse(any("recovery attempt " in entry.args[0] for entry in self.log.call_args_list))
        self.log.assert_any_call("agent_runner.py: interrupted")

    def test_invalid_cli_delays_fail_before_starting_the_agent(self) -> None:
        for delay in ("-1", "nan", "inf", "-inf", "1e400", "not-a-number"):
            with self.subTest(delay=delay), patch("sys.stderr"), self.assertRaises(SystemExit) as error:
                self.runner.main(["example", f"--recovery-delay-seconds={delay}"])
            self.assertEqual(error.exception.code, 2)
        self.start.assert_not_called()
        self.sleep.assert_not_called()

    def test_invalid_cli_recovery_limits_fail_before_starting_the_agent(self) -> None:
        for limit in ("-1", "1.5", "nan", "not-a-number"):
            with self.subTest(limit=limit), patch("sys.stderr"), self.assertRaises(SystemExit) as error:
                self.runner.main(["example", f"--max-recovery-attempts={limit}"])
            self.assertEqual(error.exception.code, 2)
        self.start.assert_not_called()
        self.sleep.assert_not_called()


class WstopRecoveryDelaySupervisorTest(AgentV1RecoveryDelaySupervisorTest):
    agent_name = "agent_v1_01_wstop"


class Wstop2RecoveryDelaySupervisorTest(AgentV1RecoveryDelaySupervisorTest):
    agent_name = "agent_v1_07_wstop2"


class WresetRecoveryDelaySupervisorTest(AgentV1RecoveryDelaySupervisorTest):
    agent_name = "test_arc3_agent_v1_02_wstop_wreset"


if __name__ == "__main__":
    unittest.main()
