"""Real subprocess/Unix-socket isolation; task backend is an explicit test double."""
from __future__ import annotations

import asyncio
from functools import partial
import json
import os
import shutil
from pathlib import Path
import tempfile
import unittest

from terminal_bench.catalog import BenchmarkCatalog
from terminal_bench.compat import EnvironmentError, EnvironmentStartError
from terminal_bench.environment_manager import EnvironmentManager
from terminal_bench.process_session import ProcessSession
from terminal_bench.runtime import RuntimeService


class ProcessIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tb_proc_")
        self.root = Path(self.temp.name)
        repo = self.root / "repo"
        for name in ["quick", "blocked-start", "blocked-finish", "failed-start", "timeout-verify", "blocked-timeout-verify"]:
            task = repo / "tasks" / name
            task.mkdir(parents=True)
            (task / "task.toml").write_text('version = "1.0"\n')
            (task / "instruction.md").write_text("SIMULATED TEST TASK. NOT A BENCHMARK TASK.")
        repo3, repo4 = self.root / "repo3", self.root / "repo4"
        shutil.copytree(repo, repo3)
        shutil.copytree(repo, repo4)
        self.service = RuntimeService(
            socket_path=self.root / "runtime.sock", catalog=BenchmarkCatalog(repo, tb3_root=repo3, tb4_root=repo4),
            output_dir=self.root / "out", runs_dir=self.root / "run",
            session_factory=partial(ProcessSession, worker_factory="tests.process_fake:ControlledSession"),
            cleanup_grace_seconds=0.25, finish_grace_seconds=5,
        )
        await self.service.start()
        self.managers = []

    async def asyncTearDown(self):
        for directory in (self.root / "out").iterdir():
            if directory.is_dir():
                (directory / "release").touch()
        await self.service.stop()
        for session in self.service.sessions.values():
            if session.process is not None:
                self.assertIsNotNone(session.process.returncode, "Worker leaked after shutdown")
        self.temp.cleanup()

    def manager(self):
        manager = EnvironmentManager({"socket_path": str(self.root / "runtime.sock")})
        self.managers.append(manager)
        return manager

    async def start_manager(self, manager, name="quick"):
        return await asyncio.wait_for(asyncio.to_thread(manager.start_environment, "terminal-bench-2.1-" + name), 15)

    async def file(self, manager, name):
        path = self.root / "out" / manager.session_id / name
        for _ in range(1000):
            if path.exists():
                return path
            await asyncio.sleep(0.01)
        self.fail("Expected worker marker: " + str(path))

    def release(self, manager):
        (self.root / "out" / manager.session_id / "release").touch()

    async def quick_complete(self):
        manager = self.manager()
        start = await self.start_manager(manager)
        pid = json.loads(start.observation.files["worker.json"])["pid"]
        self.assertNotEqual(pid, os.getpid())
        self.assertFalse((await asyncio.to_thread(manager.move, {"id": "terminal_new"})).terminal)
        final = await asyncio.to_thread(manager.move, {"id": "finish"})
        self.assertTrue(final.terminal)
        self.assertNotIn("result.json", final.files)
        await self.service.completions[manager.session_id]
        result = json.loads((self.root / "out" / manager.session_id / "result.json").read_text())
        self.assertEqual(result["worker_pid"], pid)
        self.assertEqual(result["session_id"], manager.session_id)
        worker = self.service.sessions[manager.session_id]
        self.assertIsNotNone(worker.process.returncode)
        await asyncio.to_thread(manager.stop)
        return result

    async def test_genuinely_blocked_start_does_not_block_other_managers(self):
        a = self.manager()
        starting = asyncio.create_task(self.start_manager(a, "blocked-start"))
        marker = await self.file(a, "blocked")
        b = await asyncio.wait_for(self.quick_complete(), 10)
        self.assertFalse(starting.done())
        self.assertNotEqual(int(marker.read_text()), b["worker_pid"])
        self.release(a)
        await starting
        await asyncio.to_thread(a.stop)

    async def test_blocked_action_does_not_block_other_managers(self):
        a = self.manager()
        await self.start_manager(a)
        blocked = asyncio.create_task(asyncio.to_thread(a.move, {"id": "terminal_send", "terminal_id": "t1",
                                                               "keys": [{"text": "TEST_BLOCK"}]}))
        await self.file(a, "blocked")
        await asyncio.wait_for(self.quick_complete(), 10)
        self.assertFalse(blocked.done())
        self.release(a)
        await blocked
        await asyncio.to_thread(a.stop)

    async def test_blocked_finish_does_not_block_other_managers(self):
        a = self.manager()
        await self.start_manager(a, "blocked-finish")
        finishing = asyncio.create_task(asyncio.to_thread(a.move, {"id": "finish"}))
        await self.file(a, "blocked")
        await asyncio.wait_for(self.quick_complete(), 10)
        self.assertTrue(finishing.done())
        self.assertTrue((await finishing).terminal)
        self.assertFalse((self.root / "out" / a.session_id / "result.json").exists())
        self.release(a)
        await self.service.completions[a.session_id]
        self.assertEqual(json.loads((self.root / "out" / a.session_id / "result.json").read_text())["status"], "finished")
        await asyncio.to_thread(a.stop)

    async def test_force_close_bypasses_blocked_action_and_preserves_other_session(self):
        a = self.manager()
        await self.start_manager(a)
        blocked = asyncio.create_task(asyncio.to_thread(a.move, {"id": "terminal_send", "terminal_id": "t1",
                                                               "keys": [{"text": "TEST_BLOCK"}]}))
        await self.file(a, "blocked")
        await asyncio.wait_for(asyncio.to_thread(a.stop), 10)
        with self.assertRaises(EnvironmentError):
            await blocked
        result = json.loads((await self.file(a, "result.json")).read_text())
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["cleanup_required"])
        await self.quick_complete()

    async def test_force_close_can_cancel_blocked_start(self):
        a = self.manager()
        pending = asyncio.create_task(self.start_manager(a, "blocked-start"))
        await self.file(a, "blocked")
        await asyncio.wait_for(asyncio.to_thread(a.stop), 10)
        with self.assertRaises(EnvironmentStartError):
            await pending
        self.assertTrue(json.loads((await self.file(a, "result.json")).read_text())["cleanup_required"])
        await self.quick_complete()

    async def test_crashed_worker_does_not_break_other_session(self):
        a, b = self.manager(), self.manager()
        await self.start_manager(a)
        await self.start_manager(b)
        with self.assertRaises(EnvironmentError):
            await asyncio.to_thread(a.move, {"id": "terminal_send", "terminal_id": "t1", "keys": [{"text": "TEST_CRASH"}]})
        self.assertTrue((await asyncio.to_thread(b.move, {"id": "finish"})).terminal)
        await asyncio.to_thread(a.stop)
        await asyncio.to_thread(b.stop)
        self.assertEqual(json.loads((await self.file(a, "result.json")).read_text())["status"], "error")

    async def test_failed_start_result_survives_and_worker_is_reaped(self):
        a = self.manager()
        with self.assertRaises(EnvironmentStartError):
            await self.start_manager(a, "failed-start")
        result = json.loads((await self.file(a, "result.json")).read_text())
        self.assertEqual(result["status"], "error")
        self.assertIsNotNone(self.service.sessions[a.session_id].process.returncode)
        await self.quick_complete()

    async def test_concurrent_stops_are_idempotent(self):
        a = self.manager()
        await self.start_manager(a)
        session = self.service.sessions[a.session_id]
        await asyncio.gather(session.close(), session.close(), session.close())
        self.assertTrue(session.closed)
        self.assertIsNotNone(session.process.returncode)
        self.assertEqual(json.loads((await self.file(a, "result.json")).read_text())["status"], "finished")

    async def test_three_benchmark_versions_use_separate_workers_and_results(self):
        managers = [self.manager() for _ in range(3)]
        versions = ["2.1", "3.0", "4.0"]
        starts = await asyncio.gather(*[
            asyncio.to_thread(m.start_environment, f"terminal-bench-{v}-quick")
            for m, v in zip(managers, versions)
        ])
        pids = [json.loads(start.observation.files["worker.json"])["pid"] for start in starts]
        self.assertEqual(len(set(pids)), 3)
        finals = await asyncio.gather(*[asyncio.to_thread(m.move, {"id": "finish"}) for m in managers])
        for manager, version, final in zip(managers, versions, finals):
            self.assertNotIn("result.json", final.files)
            await self.service.completions[manager.session_id]
            result = json.loads((self.root / "out" / manager.session_id / "result.json").read_text())
            self.assertEqual(result["benchmark"], f"terminal-bench-{version}")
            self.assertEqual(result["session_id"], manager.session_id)
            record = json.loads((self.root / "out" / manager.session_id / "result.json").read_text())
            self.assertEqual(record["game_id"], f"terminal-bench-{version}-quick")

    async def test_tb3_blocked_start_does_not_block_tb4(self):
        a, b = self.manager(), self.manager()
        pending = asyncio.create_task(asyncio.to_thread(a.start_environment,
                                                       "terminal-bench-3.0-blocked-start"))
        await self.file(a, "blocked")
        await asyncio.wait_for(asyncio.to_thread(b.start_environment, "terminal-bench-4.0-quick"), 10)
        final = await asyncio.wait_for(asyncio.to_thread(b.move, {"id": "finish"}), 10)
        self.assertTrue(final.terminal)
        await self.service.completions[b.session_id]
        self.assertEqual(json.loads((self.root / "out" / b.session_id / "result.json").read_text())["benchmark"], "terminal-bench-4.0")
        self.assertFalse(pending.done())
        self.release(a)
        await pending
        await asyncio.to_thread(a.stop)

    async def test_close_after_timeout_uses_verification_grace_in_supervisor(self):
        a = self.manager()
        await self.start_manager(a, "timeout-verify")
        await self.file(a, "verifying")
        worker = self.service.sessions[a.session_id]
        self.assertFalse(worker.submitted)
        closing = asyncio.create_task(asyncio.to_thread(a.stop))
        await asyncio.sleep(0.45)  # Greater than the 0.25 s abort-cleanup grace.
        self.assertTrue(closing.done(), "Manager stop must acknowledge before grading")
        self.assertIsNone(worker.process.returncode)
        await self.quick_complete()
        self.release(a)
        await asyncio.wait_for(closing, 5)
        result = json.loads((await self.file(a, "result.json")).read_text())
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["termination_reason"], "agent_timeout")
        self.assertNotIn("cleanup_required", result)

    async def test_blocked_timeout_verifier_keeps_its_grace_without_blocking_peers(self):
        a = self.manager()
        await self.start_manager(a, "blocked-timeout-verify")
        await self.file(a, "blocked")
        closing = asyncio.create_task(asyncio.to_thread(a.stop))
        await asyncio.sleep(0.45)
        self.assertTrue(closing.done())
        await self.quick_complete()
        self.release(a)
        await asyncio.wait_for(closing, 5)
        self.assertEqual(json.loads((await self.file(a, "result.json")).read_text())["status"], "finished")

    async def test_native_completion_is_reported_and_reaped_without_an_agent_poll(self):
        manager = self.manager()
        await self.start_manager(manager, "timeout-verify")
        await self.file(manager, "verifying")
        self.release(manager)
        await asyncio.wait_for(self.service.completions[manager.session_id], 5)
        self.assertEqual(self.service.outcomes[manager.session_id], "validated")
        self.assertIsNotNone(self.service.sessions[manager.session_id].process.returncode)
        late = await asyncio.to_thread(manager.move, {"id": "terminal_new"})
        self.assertTrue(late.terminal)
        self.assertNotIn("result.json", late.files)

    async def test_verification_grace_is_still_bounded(self):
        a = self.manager()
        await self.start_manager(a, "blocked-timeout-verify")
        await self.file(a, "blocked")
        worker = self.service.sessions[a.session_id]
        worker.finish_grace_seconds = 0.45
        await asyncio.wait_for(asyncio.to_thread(a.stop), 5)
        result = json.loads((await self.file(a, "result.json")).read_text())
        self.assertEqual(result["exception_type"], "WorkerForcedTermination")
        self.assertTrue(result["cleanup_required"])
        await self.quick_complete()
