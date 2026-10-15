"""Lifecycle contract tests with a fake Harbor lifecycle and scripted sandbox.

These exercise our actual custom-agent/queue/terminal code, NOT Docker or Harbor.
"""
from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from enum import Enum
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from terminal_bench.harbor_runner import HarborSession, summarize_result
from terminal_bench.protocol import RuntimeFault, decode_observation
from terminal_bench.settings import TerminalSettings
from tests.fakes import ScriptedEnvironment


class _BaseAgent:
    def __init__(self, logs_dir, **kwargs):
        self.logs_dir = logs_dir


class _Config:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class BridgeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tb_bridge_")
        self.root = Path(self.temp.name)
        self.task = self.root / "task"
        self.task.mkdir()
        (self.task / "task.toml").write_text('version = "1.0"\n')
        (self.task / "instruction.md").write_text("RAW TEXT MUST NOT OVERRIDE HARBOR INSTRUCTION")
        self.env = ScriptedEnvironment()
        self.created = []
        self.grade_entered = asyncio.Event()
        self.grade_release = asyncio.Event()
        self.grade_release.set()
        self.cleaned = False
        self.force_timeout = False
        self.owner = self
        self.verifier_fails = False
        self.reward = 1.0
        self.agent_timeout_seconds = 900.0

        class TrialEvent(Enum):
            AGENT_END = "agent-end"
            VERIFICATION_START = "verification-start"


        owner = self
        class FakeTrial:
            @classmethod
            async def create(cls, config):
                owner.created.append(config)
                instance = cls()
                instance.config = config
                instance._agent_timeout_sec = 0.05 if owner.force_timeout else owner.agent_timeout_seconds
                instance.hooks = {}
                module, symbol = config.agent["import_path"].split(":")
                agent_class = getattr(importlib.import_module(module), symbol)
                instance.agent = agent_class(logs_dir=owner.root / "agent", **config.agent["kwargs"])
                return instance

            def add_hook(self, event, hook):
                self.hooks.setdefault(event, []).append(hook)

            async def emit(self, event, data):
                raw = data.get("exception_info")
                result = SimpleNamespace(exception_info=SimpleNamespace(**raw) if raw else None)
                for hook in self.hooks.get(event, []):
                    await hook(SimpleNamespace(event=event, result=result))

            async def run(self):
                # Match pinned Harbor's significant control flow: timeout is
                # recorded but still goes through collection and verification.
                data = {"exception_info": None, "verifier_result": None}
                try:
                    await self.agent.setup(owner.env)
                    try:
                        execute = self.agent.run("EXACT PUBLIC HARBOR INSTRUCTION\n", owner.env, SimpleNamespace())
                        try:
                            await asyncio.wait_for(execute, self._agent_timeout_sec)
                        finally:
                            await self.emit(TrialEvent.AGENT_END, data)
                    except asyncio.TimeoutError:
                        data["exception_info"] = {"exception_type": "AgentTimeoutError"}
                    await self.emit(TrialEvent.VERIFICATION_START, data)
                    owner.grade_entered.set()
                    data["verifier"] = {"started_at": "simulated-verifier-start"}
                    await owner.grade_release.wait()
                    if owner.verifier_fails:
                        raise RuntimeError("Simulated verifier failure")
                    data["verifier_result"] = {"rewards": {"reward": owner.reward}}
                except Exception as exc:
                    # Native Harbor preserves the first exception, including a
                    # timeout that precedes a verifier failure.
                    if data["exception_info"] is None:
                        data["exception_info"] = {"exception_type": type(exc).__name__}
                finally:
                    owner.cleaned = True
                return SimpleNamespace(model_dump=lambda **kwargs: data)

        modules = {}
        for name in ["harbor", "harbor.agents", "harbor.agents.base", "harbor.models",
                     "harbor.models.task", "harbor.models.task.config", "harbor.models.trial", "harbor.models.trial.config", "harbor.trial", "harbor.trial.trial", "harbor.trial.hooks"]:
            module = ModuleType(name)
            module.__path__ = []
            modules[name] = module
        modules["harbor.agents.base"].BaseAgent = _BaseAgent
        modules["harbor.models.trial.config"].TrialConfig = _Config
        modules["harbor.models.task.config"].TaskConfig = SimpleNamespace(
            model_validate=lambda value: SimpleNamespace(environment=SimpleNamespace(build_timeout_sec=600)),
        )
        modules["harbor.trial.trial"].Trial = FakeTrial
        modules["harbor.trial.hooks"].TrialEvent = TrialEvent
        self.previous_agent_module = sys.modules.pop("terminal_bench.harbor_agent", None)
        self.patch = patch.dict(sys.modules, modules)
        self.patch.start()
        self.session = HarborSession(session_id=uuid4().hex, task_id="task", task_path=self.task,
                                    output_dir=self.root / "output", workspace_dir=self.root / "workspace",
                                    settings=TerminalSettings(install_tmux=False))

    async def asyncTearDown(self):
        self.grade_release.set()
        await self.session.close()
        sys.modules.pop("terminal_bench.harbor_agent", None)
        self.patch.stop()
        if self.previous_agent_module is not None:
            sys.modules["terminal_bench.harbor_agent"] = self.previous_agent_module
        self.temp.cleanup()

    async def test_public_instruction_and_custom_agent_configuration(self):
        start = await self.session.start()
        self.assertIn("EXACT PUBLIC HARBOR INSTRUCTION", start["environment_description"])
        self.assertNotIn("RAW TEXT", start["environment_description"])
        cfg = self.created[0]
        self.assertEqual(cfg.agent["import_path"], "terminal_bench.harbor_agent:RemoteTerminalAgent")
        self.assertEqual(cfg.verifier, {"disable": False})
        self.assertEqual(cfg.environment["delete"], True)
        self.assertNotIn("override_timeout_sec", cfg.agent)
        self.assertFalse(hasattr(cfg, "agent_timeout_multiplier"))
        self.assertFalse(hasattr(cfg, "verifier_timeout_multiplier"))
        self.assertEqual(cfg.environment_build_timeout_multiplier, 6.0)
        await self.session.move({"id": "terminal_send", "terminal_id": "t1",
                                 "keys": [{"text": "literal"}, {"key": "Enter"}]})
        self.assertEqual(bytes(self.env.panes["%0"]["input"]), b"literal")
        self.assertEqual(self.env.panes["%0"]["keys"], ["Enter"])

    async def test_finish_returns_before_verifier_and_keeps_results_on_host(self):
        await self.session.start()
        self.grade_release.clear()
        pending = asyncio.create_task(self.session.move({"id": "finish"}))
        await self.grade_entered.wait()
        final = await asyncio.wait_for(pending, 0.5)
        self.assertFalse(self.cleaned)
        self.assertNotIn("result.json", decode_observation(final["observation"])[0])
        self.grade_release.set()
        await self.session.runner
        files, terminal = decode_observation(final["observation"])
        self.assertTrue(terminal)
        self.assertTrue(self.cleaned)
        self.assertNotIn("result.json", files)
        self.assertEqual(self.session.final["rewards"], {"reward": 1.0})
        self.assertTrue((self.session.audit_dir / "result.json").is_file())

    async def test_close_after_submission_waits_instead_of_canceling_verifier(self):
        await self.session.start()
        self.grade_release.clear()
        finishing = asyncio.create_task(self.session.move({"id": "finish"}))
        await self.grade_entered.wait()
        closing = asyncio.create_task(self.session.close())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        self.grade_release.set()
        await asyncio.gather(finishing, closing)
        self.assertEqual(self.session.final["status"], "finished")

    async def test_unfinished_close_grades_partial_solution_and_cleans(self):
        await self.session.start()
        await self.session.close()
        await self.session.close()
        self.assertTrue(self.cleaned)
        self.assertEqual(self.session.final["status"], "finished")
        self.assertEqual(self.session.final["termination_reason"], "agent_stopped")

    async def test_setup_failure_returns_error_and_cleans(self):
        self.env.tmux_available = False
        with self.assertRaises(RuntimeFault):
            await self.session.start()
        self.assertTrue(self.cleaned)
        self.assertEqual(self.session.final["status"], "error")

    async def test_native_timeout_is_visible_as_terminal_observation(self):
        self.force_timeout = True
        await self.session.start()
        await self.session.runner
        result = await self.session.move({"id": "terminal_read", "terminal_id": "t1"})
        files, terminal = decode_observation(result["observation"])
        self.assertTrue(terminal)
        self.assertNotIn("result.json", files)
        self.assertNotIn("time_budget.json", files)
        self.assertEqual(self.session.final["exception_type"], "AgentTimeoutError")

    async def test_invalid_command_does_not_stop_agent_loop(self):
        await self.session.start()
        with self.assertRaises(RuntimeFault):
            await self.session.move({"id": "terminal_send", "terminal_id": "t1", "keys": [{"key": "INVALID"}]})
        result = await self.session.move({"id": "terminal_read", "terminal_id": "t1"})
        self.assertFalse(result["observation"]["terminal"])

    async def test_cancel_before_runner_begins(self):
        self.session.runner = asyncio.create_task(asyncio.sleep(3600))
        await self.session.close()
        self.assertTrue(self.session.state.ready.is_set())
        self.assertEqual(self.session.final["status"], "aborted")

    async def test_timeout_still_runs_verifier_and_preserves_positive_reward(self):
        self.force_timeout = True
        await self.session.start()
        await self.session.runner
        self.assertTrue(self.grade_entered.is_set())
        self.assertEqual(self.session.final["status"], "finished")
        self.assertEqual(self.session.final["termination_reason"], "agent_timeout")
        self.assertEqual(self.session.final["verification_status"], "finished")
        self.assertEqual(self.session.final["rewards"], {"reward": 1.0})
        self.assertFalse(self.session.submitted)

    async def test_close_after_timeout_preserves_verification_without_finish(self):
        self.force_timeout = True
        self.grade_release.clear()
        await self.session.start()
        await self.grade_entered.wait()
        self.assertFalse(self.session.submitted)
        lifecycle = json.loads((self.session.audit_dir / "lifecycle.json").read_text())
        self.assertEqual(lifecycle["phase"], "verifying")
        self.assertTrue(lifecycle["preserve_finalization"])
        closing = asyncio.create_task(self.session.close())
        await asyncio.sleep(0.02)
        self.assertFalse(closing.done())
        self.assertFalse(self.cleaned)
        self.grade_release.set()
        await asyncio.wait_for(closing, 2)
        self.assertEqual(self.session.final["status"], "finished")
        self.assertEqual(self.session.final["rewards"]["reward"], 1.0)
        self.assertEqual(self.session.final["exception_type"], "AgentTimeoutError")

    async def test_timeout_with_zero_reward_is_finished_not_infrastructure_error(self):
        self.force_timeout, self.reward = True, 0.0
        await self.session.start()
        await self.session.runner
        self.assertEqual(self.session.final["status"], "finished")
        self.assertEqual(self.session.final["rewards"], {"reward": 0.0})

    async def test_verifier_failure_after_timeout_is_not_silently_finished(self):
        self.force_timeout, self.verifier_fails = True, True
        await self.session.start()
        await self.session.runner
        self.assertEqual(self.session.final["exception_type"], "AgentTimeoutError")
        self.assertEqual(self.session.final["status"], "error")
        self.assertEqual(self.session.final["verification_status"], "error")
        self.assertIsNone(self.session.final["rewards"])

    async def test_late_action_ends_game_without_waiting_or_touching_terminal(self):
        self.force_timeout = True
        self.grade_release.clear()
        await self.session.start()
        await self.grade_entered.wait()
        calls = len(self.env.calls)
        pending = asyncio.create_task(self.session.move({"id": "terminal_new"}))
        await asyncio.sleep(0.02)
        self.assertTrue((await asyncio.wait_for(pending, 0.5))["observation"]["terminal"])
        self.assertEqual(len(self.env.calls), calls)
        self.assertEqual(self.session.state.queue.qsize(), 0)
        self.grade_release.set()
        self.assertTrue((await pending)["observation"]["terminal"])

    async def test_native_budget_is_described_but_timing_stays_in_host_records(self):
        # Intentionally different from task.toml: use the resolved native budget.
        self.agent_timeout_seconds = 17.25
        start = await self.session.start()
        self.assertIn("Solving time budget: 17.25 seconds (wall-clock).", start["environment_description"])
        initial = self.session.state.lifecycle.time_budget()
        self.assertEqual(initial["agent_timeout_seconds"], 17.25)
        self.assertIsNotNone(initial["deadline_at"])
        await asyncio.sleep(0.02)
        moved = await self.session.move({"id": "terminal_read", "terminal_id": "t1"})
        timing = self.session.state.lifecycle.time_budget()
        self.assertLess(timing["remaining_seconds"], initial["remaining_seconds"])
        self.assertEqual(timing["started_at"], initial["started_at"])
        final = await self.session.move({"id": "finish"})
        for response in (start, moved, final):
            files, _ = decode_observation(response["observation"])
            self.assertNotIn("time_budget.json", files)
            self.assertNotIn("result.json", files)
            self.assertNotRegex(json.dumps(files).lower(), r"harbor|terminal[-_ ]bench|clock_note")
        await self.session.runner
        saved = json.loads((self.session.audit_dir / "result.json").read_text())
        self.assertEqual(saved["time_budget"]["agent_timeout_seconds"], 17.25)
        self.assertEqual(saved["time_budget"]["termination_reason"], "finish")
        self.assertNotIn("clock_note", saved["time_budget"])

    async def test_close_in_pending_agent_cancellation_does_not_cancel_parent_trial(self):
        await self.session.start()
        self.grade_release.clear()
        # Simulates the scheduling gap between wait_for cancelling run() and
        # its finally clause marking solving ended. NOT an official timeout.
        agent_task = self.session.state.lifecycle._agent_task
        agent_task.cancel()
        self.assertTrue(self.session.state.lifecycle.preserve_finalization)
        # Turn run()'s cancellation into the timeout shape in the test driver by
        # testing the guard directly; native flow is exercised above.
        self.assertFalse(self.session.state.lifecycle.abort_requested)
        self.grade_release.set()
        await self.session.runner


class SummaryTests(unittest.TestCase):
    def test_zero_reward_is_completed_not_success(self):
        summary = summarize_result({"verifier_result": {"rewards": {"reward": 0.0}}}, "s", "t")
        self.assertEqual(summary["status"], "finished")
        self.assertEqual(summary["rewards"]["reward"], 0.0)

    def test_missing_reward_is_an_error(self):
        self.assertEqual(summarize_result({}, "s", "t")["status"], "error")

    def test_private_traceback_is_not_exposed(self):
        raw = {"exception_info": {"exception_type": "Example", "exception_message": "/private/key",
                                  "exception_traceback": "SECRET"}}
        self.assertNotIn("SECRET", json.dumps(summarize_result(raw, "s", "t")))
        self.assertNotIn("/private", json.dumps(summarize_result(raw, "s", "t")))

    def test_timeout_rewards_positive_or_zero_are_kept(self):
        for reward in [0.0, 1.0, 0.5]:
            with self.subTest(reward=reward):
                result = summarize_result({"exception_info": {"exception_type": "AgentTimeoutError"},
                    "verifier_result": {"rewards": {"reward": reward}}}, "s", "t")
                self.assertEqual(result["status"], "finished")
                self.assertEqual(result["termination_reason"], "agent_timeout")
                self.assertEqual(result["verification_status"], "finished")
                self.assertEqual(result["rewards"]["reward"], reward)
                self.assertNotIn("infrastructure failure", result["note"])

    def test_other_errors_are_not_reclassified_by_presence_of_reward(self):
        result = summarize_result({"exception_info": {"exception_type": "ExampleFailure"},
            "verifier_result": {"rewards": {"reward": 1.0}}}, "s", "t")
        self.assertEqual(result["status"], "error")

    def test_verifier_timeout_is_a_verification_error(self):
        result = summarize_result({"exception_info": {"exception_type": "VerifierTimeoutError"}},
                                  "s", "t", termination_reason="finish")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["termination_reason"], "finish")
        self.assertEqual(result["verification_status"], "error")
        self.assertIsNone(result["rewards"])

    def test_malformed_and_nonfinite_rewards_are_not_successful_evaluations(self):
        for rewards in [None, [], {}, {"reward": "1"}, {"reward": True},
                        {"reward": float("nan")}, {"reward": float("inf")}]:
            result = summarize_result({"verifier_result": {"rewards": rewards}}, "s", "t",
                                      verification_started=True)
            self.assertEqual(result["status"], "error")
            self.assertIsNone(result["rewards"])
            self.assertEqual(result["verification_status"], "error")

    def test_timeout_without_verifier_reward_is_still_an_error(self):
        result = summarize_result({"exception_info": {"exception_type": "AgentTimeoutError"},
                                   "verifier": {"started_at": "yes"}}, "s", "t")
        self.assertEqual(result["termination_reason"], "agent_timeout")
        self.assertEqual(result["verification_status"], "error")
        self.assertEqual(result["status"], "error")
