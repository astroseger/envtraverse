"""Clock/close-race tests: no Harbor, Docker or real-time budget modifications."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from terminal_bench.description import environment_description
from terminal_bench.lifecycle import SessionLifecycle
from terminal_bench.process_session import ProcessSession
from terminal_bench.records import atomic_json
from terminal_bench.settings import TerminalSettings


class ClockTests(unittest.TestCase):
    def test_elapsed_time_does_not_reset_and_stops_at_solving_end(self):
        state = SessionLifecycle()
        state.configure_timeout(90)
        with patch("terminal_bench.lifecycle.time.monotonic", return_value=100):
            state.begin_solving()
        with patch("terminal_bench.lifecycle.time.monotonic", return_value=115):
            self.assertEqual(state.time_budget()["remaining_seconds"], 75)
            state.end_solving("finish")
        with patch("terminal_bench.lifecycle.time.monotonic", return_value=250):
            state.begin_verification()
            self.assertEqual(state.time_budget()["elapsed_seconds"], 15)
            self.assertEqual(state.time_budget()["remaining_seconds"], 75)
        self.assertTrue(state.preserve_finalization)
        self.assertNotIn("clock_note", state.record())

    def test_budget_can_be_unlimited_without_inventing_a_deadline(self):
        state = SessionLifecycle()
        state.configure_timeout(None)
        state.begin_solving()
        self.assertIsNone(state.time_budget()["remaining_seconds"])
        self.assertIsNone(state.deadline_at)
        text = environment_description("PUBLIC", 1024, time_budget=state.time_budget())
        self.assertIn("Solving time budget: unlimited.", text)
        self.assertTrue(text.endswith("PUBLIC\n--- END TASK ---\n"))

    def test_clock_cannot_be_restarted(self):
        state = SessionLifecycle()
        state.begin_solving()
        with self.assertRaises(RuntimeError):
            state.begin_solving()

    def test_abort_stays_abort_when_agent_finally_runs(self):
        state = SessionLifecycle()
        state.begin_solving()
        state.request_abort()
        state.end_solving("agent_stopped")
        self.assertEqual(state.phase, "aborting")
        self.assertEqual(state.termination_reason, "aborted")
        self.assertFalse(state.preserve_finalization)

    def test_expired_budget_guards_close_until_native_timeout_is_delivered(self):
        state = SessionLifecycle()
        state.configure_timeout(5)
        with patch("terminal_bench.lifecycle.time.monotonic", return_value=10):
            state.begin_solving()
        with patch("terminal_bench.lifecycle.time.monotonic", return_value=20):
            self.assertTrue(state.preserve_finalization)
            self.assertEqual(state.time_budget()["remaining_seconds"], 0)

    def test_invalid_resolved_budget_is_rejected(self):
        for value in [-1, 0, True, float("inf"), float("nan"), "120"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                SessionLifecycle().configure_timeout(value)


class GraceRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_phase_change_during_close_extends_existing_wait(self):
        with tempfile.TemporaryDirectory(prefix="tb_grace_") as temp:
            root = Path(temp)
            directory = root / "test-session"
            directory.mkdir()
            session = ProcessSession(session_id="test-session", task_id="task", task_path=root,
                output_dir=root, settings=TerminalSettings(), workspace_dir=root / "workspace", benchmark="test", task_name="task",
                cleanup_grace_seconds=0.15)
            released = asyncio.Event()
            async def rpc(method, **kwargs):
                self.assertEqual(method, "close")
                await released.wait()
                return {"closed": True}
            session._rpc = rpc
            pending = asyncio.create_task(session._wait_for_close())
            try:
                await asyncio.sleep(0.025)
                atomic_json(directory / "lifecycle.json", {"session_id": "test-session",
                    "preserve_finalization": True, "phase": "finalizing"})
                await asyncio.sleep(0.22)
                self.assertFalse(pending.done())
            finally:
                released.set()
            await asyncio.wait_for(pending, 1)

    async def test_prepared_task_waits_past_former_finalization_deadline(self):
        with tempfile.TemporaryDirectory(prefix="tb_finalization_") as temp:
            root = Path(temp)
            session = ProcessSession(session_id="test-session", task_id="task", task_path=root,
                output_dir=root, settings=TerminalSettings(), workspace_dir=root / "workspace",
                benchmark="test", task_name="task", cleanup_grace_seconds=0.1)
            session.ready = True
            released = asyncio.Event()
            async def rpc(method, **kwargs):
                await released.wait()
                return {"closed": True}
            session._rpc = rpc
            # Advance the supervisor's clock beyond the former two-hour grace
            # without changing the event loop clock or actually waiting hours.
            clock = Mock()
            clock.time.side_effect = [0, 10000]
            with patch("terminal_bench.process_session.asyncio.get_running_loop", return_value=clock):
                pending = asyncio.create_task(session._wait_for_close())
                try:
                    await asyncio.sleep(0.15)
                    self.assertFalse(pending.done())
                finally:
                    released.set()
                    await pending

    async def test_unrelated_lifecycle_record_cannot_extend_another_session(self):
        with tempfile.TemporaryDirectory(prefix="tb_grace_") as temp:
            root = Path(temp)
            (root / "test-session").mkdir()
            session = ProcessSession(session_id="test-session", task_id="task", task_path=root,
                output_dir=root, settings=TerminalSettings(), workspace_dir=root / "workspace", benchmark="test", task_name="task")
            atomic_json(root / "test-session" / "lifecycle.json", {"session_id": "other-session",
                "preserve_finalization": True, "phase": "verifying"})
            self.assertFalse(session._preserve_finalization())
