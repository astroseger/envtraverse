"""New uploaded API + retained loader/serializer compatibility (no latest launcher supplied)."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class UnifiedAgentContractTests(unittest.TestCase):
    def test_discovery_import_and_observation_identity(self):
        package = Path(__file__).resolve().parents[2]
        server_source = package.parent.parent
        with tempfile.TemporaryDirectory(prefix="tb_contract_") as tmp:
            server = Path(tmp) / "server"
            server.mkdir()
            for filename in ("environment_api.py", "artifacts.py", "environment_loader.py"):
                shutil.copyfile(server_source / filename, server / filename)
            shutil.copytree(package, server / "environments/terminal_bench",
                            ignore=shutil.ignore_patterns("__pycache__", "host_runtime"))
            (server / "environments/__init__.py").write_text("")
            code = '''
import sys
sys.path.insert(0, sys.argv[1])
import environment_loader, environment_api, artifacts
assert "terminal_bench" in environment_loader.available_environments()
manager = environment_loader.load_environment_manager("terminal_bench")
import environments.terminal_bench.environment_manager as bridge
assert bridge.Observation is environment_api.Observation
assert bridge.StartResult is environment_api.StartResult
encoded = artifacts.serialize_observation(bridge.Observation(files={"x.bin": b"\\x00\\xff"}))
assert encoded == [{"name":"x.bin", "content_base64":"AP8="}]
from dataclasses import fields
assert [f.name for f in fields(bridge.StartResult)] == ["environment_description", "observation"]
instance = bridge.EnvironmentManager({"socket_path": "/nonexistent/test.sock"})
assert callable(instance.move)
assert instance.client.timeout is None
instance.stop()
assert "harbor" not in sys.modules
print("New single-game API + retained loader/serializer: OK; no Harbor imported")
'''
            result = subprocess.run([sys.executable, "-c", code, str(server)],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("no Harbor imported", result.stdout)
