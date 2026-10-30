"""Opt-in real Harbor/Docker timeout tests using ONLY a temporary smoke fixture.

The copy receives a short solving budget and a slow verifier to expose the close
race. Official benchmark task files and task budgets are never changed. Results
can be retained with TB_TIMEOUT_OUTPUT (the convenience script sets it).
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from terminal_bench.catalog import BenchmarkCatalog, BUILTIN_SMOKE, SMOKE_GAME
from terminal_bench.environment_manager import EnvironmentManager
from terminal_bench.runtime import RuntimeService
from terminal_bench.smoke import _send, _wait_for


@unittest.skipUnless(
    os.environ.get("TB_RUN_DOCKER_TIMEOUT") == "1" and shutil.which("docker") is not None
    and importlib.util.find_spec("harbor") is not None,
    "Native timeout tests are opt-in and require Docker/Harbor; simulated tests are separate",
)
class LiveTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from terminal_bench.harbor_runner import check_harbor
        check_harbor()
        self.temp = tempfile.TemporaryDirectory(prefix="tb_timeout_live_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        fixture = self.root / "smoke-fixture"
        shutil.copytree(BUILTIN_SMOKE, fixture)
        config = fixture / "task.toml"
        text = config.read_text()
        if "timeout_sec = 120.0" not in text:
            raise AssertionError("Smoke fixture budget changed; update this synthetic test explicitly")
        config.write_text(text.replace("timeout_sec = 120.0", "timeout_sec = 12.0", 1))
        verifier = fixture / "tests" / "test.sh"
        lines = verifier.read_text().splitlines(keepends=True)
        verifier.write_text(lines[0] + "sleep 6\n" + "".join(lines[1:]))
        self.output = Path(os.environ.get("TB_TIMEOUT_OUTPUT", self.root / "results")).absolute()
        self.service = RuntimeService(socket_path=self.root / "runtime.sock", output_dir=self.output, runs_dir=self.root / "run",
            catalog=BenchmarkCatalog(smoke_task=fixture), cleanup_grace_seconds=5)
        self.addAsyncCleanup(self.service.stop)
        await self.service.start()
        self.manager = EnvironmentManager({"socket_path": str(self.root / "runtime.sock")})
        self.addAsyncCleanup(asyncio.to_thread, self.manager.stop)
        print("NATIVE TIMEOUT TEST session:", self.manager.session_id, "results:", self.output, flush=True)
        self.start = await asyncio.to_thread(self.manager.start_environment, SMOKE_GAME)
        self.assertNotIn("time_budget.json", self.start.observation.files)

    async def await_verification(self):
        path = self.output / self.manager.session_id / "lifecycle.json"
        for _ in range(600):
            data = json.loads(path.read_text())
            if data["phase"] == "verifying":
                self.assertEqual(data["termination_reason"], "agent_timeout")
                return
            if data["phase"] == "finished":
                self.fail("Trial ended before verification could be observed: " + path.read_text())
            await asyncio.sleep(0.05)
        self.fail("Timeout did not lead to verification")

    async def test_native_timeout_then_stop_preserves_successful_verifier_reward(self):
        await asyncio.to_thread(_send, self.manager,
            "printf 'hello from terminal bench\\n' > /app/answer.txt; printf 'TB_%s\\n' SOLUTION_READY")
        await asyncio.to_thread(_wait_for, self.manager, "TB_SOLUTION_READY")
        # No finish call. Let the actual native deadline end solving.
        await self.await_verification()
        await asyncio.wait_for(asyncio.to_thread(self.manager.stop), 2)
        await self.service.completions[self.manager.session_id]
        result = json.loads((self.output / self.manager.session_id / "result.json").read_text())
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["termination_reason"], "agent_timeout")
        self.assertEqual(result["verification_status"], "finished")
        self.assertEqual(result["exception_type"], "AgentTimeoutError")
        self.assertEqual(result["rewards"]["reward"], 1.0)
        self.assertFalse(result["cleanup_required"])

    async def test_native_timeout_without_solution_returns_zero_on_next_read(self):
        await self.await_verification()
        final = await asyncio.to_thread(self.manager.move, {"id": "terminal_read", "terminal_id": "t1"})
        self.assertTrue(final.terminal)
        self.assertNotIn("result.json", final.files)
        self.assertNotIn("time_budget.json", final.files)
        await self.service.completions[self.manager.session_id]
        result = json.loads((self.output / self.manager.session_id / "result.json").read_text())
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["termination_reason"], "agent_timeout")
        self.assertEqual(result["verification_status"], "finished")
        self.assertEqual(result["rewards"]["reward"], 0.0)
