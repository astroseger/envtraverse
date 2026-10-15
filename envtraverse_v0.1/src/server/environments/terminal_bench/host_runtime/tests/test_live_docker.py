"""Opt-in real Docker + Harbor test. This is NOT an official benchmark task."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from terminal_bench.runtime import RuntimeService
from terminal_bench.smoke import run_smoke, _send, _wait_for
from terminal_bench.catalog import BenchmarkCatalog, BUILTIN_SMOKE, SMOKE_GAME
from terminal_bench.environment_manager import EnvironmentManager


@unittest.skipUnless(
    os.environ.get("TB_RUN_DOCKER_SMOKE") == "1" and
    shutil.which("docker") is not None and
    importlib.util.find_spec("harbor") is not None,
    "Real Docker smoke is opt-in and requires Docker/Harbor; not executed by mock tests",
)
class LiveDockerSmoke(unittest.IsolatedAsyncioTestCase):
    async def test_single_real_environment_actions_file_and_verifier(self):
        from terminal_bench.harbor_runner import check_harbor
        check_harbor()
        with tempfile.TemporaryDirectory(prefix="tb_live_") as temp:
            root = Path(temp)
            service = RuntimeService(
                socket_path=root / "runtime.sock",
                output_dir=root / "results", runs_dir=root / "run",
            )
            try:
                await service.start()
                result = await asyncio.to_thread(run_smoke, root / "runtime.sock", output_dir=root / "results")
                self.assertEqual(result["rewards"]["reward"], 1.0)
            finally:
                await service.stop()

    async def test_parallel_finish_and_early_stop_grade_in_background(self):
        with tempfile.TemporaryDirectory(prefix="tb_live_parallel_") as temp:
            root = Path(temp)
            fixture = root / "fixture"
            shutil.copytree(BUILTIN_SMOKE, fixture)
            verifier = fixture / "tests/test.sh"
            lines = verifier.read_text().splitlines(keepends=True)
            verifier.write_text(lines[0] + "sleep 6\n" + "".join(lines[1:]))
            service = RuntimeService(socket_path=root / "runtime.sock", output_dir=root / "results", runs_dir=root / "run",
                                     catalog=BenchmarkCatalog(smoke_task=fixture))
            await service.start()
            managers = [EnvironmentManager({"socket_path": str(root / "runtime.sock")}) for _ in range(2)]
            try:
                starts = await asyncio.gather(*(asyncio.to_thread(m.start_environment, SMOKE_GAME) for m in managers))
                for start in starts:
                    wrapper = start.environment_description.split("--- BEGIN TASK ---", 1)[0]
                    self.assertNotRegex(wrapper.lower(), r"harbor|terminal[-_ ]bench|grading|time_budget")
                    self.assertNotIn("time_budget.json", start.observation.files)
                mounted = await asyncio.to_thread(managers[0].move,
                    {"id": "get_file", "path": "/proc/self/mountinfo"})
                mount_info = mounted.files["file.bin"].decode()
                self.assertIn("/trial/", mount_info)
                self.assertNotRegex(mount_info.lower(), r"harbor|terminal[-_ ]bench")
                self.assertNotIn("time_budget.json", mounted.files)
                await asyncio.to_thread(_send, managers[0],
                    "printf 'hello from terminal bench\\n' > /app/answer.txt; printf 'TB_%s\\n' SOLUTION_READY")
                await asyncio.to_thread(_wait_for, managers[0], "TB_SOLUTION_READY")
                terminal, _ = await asyncio.wait_for(asyncio.gather(
                    asyncio.to_thread(managers[0].move, {"id": "finish"}),
                    asyncio.to_thread(managers[1].stop),
                ), 2)
                self.assertTrue(terminal.terminal)
                self.assertNotIn("result.json", terminal.files)
                self.assertNotIn("time_budget.json", terminal.files)
                self.assertFalse((root / "results" / managers[0].session_id / "result.json").exists())
                await asyncio.wait_for(asyncio.gather(*service.completions.values()), 180)
                for manager, reason, reward in zip(managers, ("finish", "agent_stopped"), (1.0, 0.0)):
                    result = json.loads((root / "results" / manager.session_id / "result.json").read_text())
                    self.assertEqual(result["termination_reason"], reason)
                    self.assertEqual(result["verification_status"], "finished")
                    self.assertEqual(result["rewards"]["reward"], reward)
            finally:
                for manager in managers:
                    await asyncio.to_thread(manager.stop)
                await service.stop()
