"""Optional test against an installed real Harbor (no Docker operations)."""
from __future__ import annotations

import importlib.util
import importlib.metadata
import tempfile
from pathlib import Path
import unittest
from uuid import uuid4


def pinned_harbor_installed():
    try:
        return importlib.metadata.version("harbor") == "0.23.0"
    except importlib.metadata.PackageNotFoundError:
        return False


@unittest.skipUnless(pinned_harbor_installed(),
                     "Pinned Harbor 0.23.0 is not installed; run host_runtime/setup.sh")
class NativeHarborContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_config_and_agent_factory(self):
        from terminal_bench.harbor_runner import check_harbor
        from terminal_bench.bridge import BRIDGES, BridgeState
        from terminal_bench.settings import TerminalSettings
        from harbor.models.trial.config import TrialConfig
        from harbor.agents.factory import AgentFactory
        check_harbor()
        from harbor.trial.trial import Trial
        from harbor.trial.hooks import TrialEvent
        self.assertTrue(callable(Trial.add_hook))
        self.assertTrue(callable(Trial._compute_agent_timeout_sec))
        self.assertEqual(TrialEvent.AGENT_END.value, "agent-end")
        self.assertEqual(TrialEvent.VERIFICATION_START.value, "verification-start")
        key = uuid4().hex
        BRIDGES[key] = BridgeState(TerminalSettings(), Path("/unused/workspace"), Path("/unused/uploads"))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config = TrialConfig(task={"path": Path(tmp)}, agent={
                    "import_path": "terminal_bench.harbor_agent:RemoteTerminalAgent",
                    "kwargs": {"bridge_key": key},
                }, environment={"type": "docker", "delete": True}, verifier={"disable": False})
                agent = AgentFactory.create_agent_from_config(config.agent, logs_dir=Path(tmp))
                self.assertEqual(agent.name(), "unified-terminal-bridge")
        finally:
            BRIDGES.pop(key, None)
