"""Test-only worker backend. Its blocking operations deliberately freeze its event loop.

No Harbor, Docker, shell execution, model, or benchmark scoring occurs here.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import time

from tests.fakes import FakeSession
from terminal_bench.protocol import observation
from terminal_bench.records import atomic_json


class ControlledSession(FakeSession):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.directory = self.output_dir / self.session_id
        self.verification = None

    def block(self):
        (self.directory / "blocked").write_text(str(os.getpid()))
        deadline = time.monotonic() + 25
        # NOT await asyncio.sleep: this genuinely blocks ALL tasks in this worker.
        while not (self.directory / "release").exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("Test did not release blocked worker")
            time.sleep(0.01)

    async def start(self):
        if self.task_name == "blocked-start":
            self.block()
        if self.task_name == "failed-start":
            raise RuntimeError("Simulated startup failure")
        result = await super().start()
        if self.task_name in {"timeout-verify", "blocked-timeout-verify"}:
            self.verification = asyncio.create_task(self.post_timeout_verification())
        result["observation"]["files"]["worker.json"] = {"text": json.dumps({"pid": os.getpid()})}
        return result

    async def move(self, action):
        if action["id"] == "terminal_send":
            text = "".join(key.get("text", "") for key in action["keys"])
            if text == "TEST_BLOCK":
                self.block()
            elif text == "TEST_CRASH":
                os._exit(17)
        return await super().move(action)

    async def post_timeout_verification(self):
        await asyncio.sleep(0.02)
        # This is a simulated lifecycle event, not an actual Harbor timeout.
        atomic_json(self.directory / "lifecycle.json", {
            "session_id": self.session_id, "phase": "verifying",
            "termination_reason": "agent_timeout", "preserve_finalization": True,
        })
        (self.directory / "verifying").touch()
        if self.task_name == "blocked-timeout-verify":
            self.block()
        else:
            while not (self.directory / "release").exists():
                await asyncio.sleep(0.01)
        atomic_json(self.directory / "result.json", {
            "session_id": self.session_id, "game_id": self.task_id,
            "status": "finished", "verification_status": "finished",
            "termination_reason": "agent_timeout", "exception_type": "AgentTimeoutError",
            "rewards": {"reward": 1.0}, "test_double": True,
        })

    async def close(self, *, reason="agent_stopped"):
        if self.closed:
            return
        if self.task_name == "blocked-finish":
            self.block()
        if self.verification is not None:
            await self.verification
        await super().close(reason=reason)
