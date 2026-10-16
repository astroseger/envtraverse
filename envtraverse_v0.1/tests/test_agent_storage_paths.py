from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

AGENT_SRC = Path(__file__).resolve().parents[1] / "src" / "agents" / "agent_v1" / "agent"
sys.path.insert(0, str(AGENT_SRC))

import agent
import session_inspector


class AgentStoragePathsTest(unittest.TestCase):
    def setUp(self) -> None:
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.run_dir = self.root / "run" / "agent_run"
        self.run_dir.parent.mkdir()
        self.client_dir = self.root / "client"
        self.client_dir.mkdir()
        self.script = self.client_dir / "client.py"
        self.script.touch()
        self.state = self.client_dir / ".client_session.json"
        self.observations = self.root / "observations"
        self.observations.mkdir()
        self.description = self.observations / "environment_description.md"
        stack.enter_context(patch.multiple(
            agent,
            CLIENT_SCRIPT_PATH=self.script,
            CLIENT_STATE_PATH=self.state,
            OBSERVATIONS_DIR=self.observations,
            ENVIRONMENT_DESCRIPTION_PATH=self.description,
            WORKSPACE_INIT_DIR=self.root / "workspace_init",
            LOG_FILE=self.root / "agent.log",
            THREAD_ID_FILE=self.root / ".codex_thread_id",
            THREAD_ID_TEMP_FILE=self.root / ".codex_thread_id.tmp",
        ))
        stack.enter_context(patch.object(session_inspector, "CLIENT_STATE_PATH", self.state))

    def test_empty_mounted_observations_and_absent_workspace_template_allow_fresh_start(self) -> None:
        agent.ensure_fresh_run_paths(self.run_dir)
        agent.prepare_agent_run(self.run_dir)
        self.assertTrue(self.run_dir.is_dir())
        self.assertEqual(list(self.run_dir.iterdir()), [])

    def test_workspace_helpers_are_still_copied(self) -> None:
        agent.WORKSPACE_INIT_DIR.mkdir()
        (agent.WORKSPACE_INIT_DIR / "helper.py").write_text("# helper\n", encoding="utf-8")
        agent.prepare_agent_run(self.run_dir)
        self.assertEqual((self.run_dir / "helper.py").read_text(encoding="utf-8"), "# helper\n")

    def test_empty_workspace_template_is_also_supported(self) -> None:
        agent.WORKSPACE_INIT_DIR.mkdir()
        agent.prepare_agent_run(self.run_dir)
        self.assertTrue(self.run_dir.is_dir())
        self.assertEqual(list(self.run_dir.iterdir()), [])

    def test_fresh_start_rejects_missing_mounts_and_old_observations_or_session(self) -> None:
        self.script.unlink()
        with self.assertRaisesRegex(FileNotFoundError, "mounts"):
            agent.ensure_fresh_run_paths(self.run_dir)
        self.script.touch()
        old_observation = self.observations / "step_0000.txt"
        old_observation.touch()
        with self.assertRaisesRegex(FileExistsError, "empty observations"):
            agent.ensure_fresh_run_paths(self.run_dir)
        old_observation.unlink()
        self.state.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(FileExistsError, "client_session"):
            agent.ensure_fresh_run_paths(self.run_dir)

    def test_inspection_and_recovery_use_separate_session_file(self) -> None:
        self.run_dir.mkdir()
        agent.THREAD_ID_FILE.write_text("thread-id", encoding="utf-8")
        with self.assertRaisesRegex(FileNotFoundError, "client_session"):
            agent.ensure_recovery_run_paths(self.run_dir)
        self.state.write_text(json.dumps({"step_index": 7, "terminal": True}), encoding="utf-8")
        self.state.chmod(0o444)
        agent.ensure_recovery_run_paths(self.run_dir)
        self.assertEqual(session_inspector.inspect_sessions().to_dict(), {"step_index": 7, "terminal": True})

    def test_recovery_paths_do_not_require_an_old_codex_thread(self) -> None:
        self.run_dir.mkdir()
        self.state.write_text(json.dumps({"step_index": 0, "terminal": False}), encoding="utf-8")
        self.assertFalse(agent.THREAD_ID_FILE.exists())
        agent.ensure_recovery_run_paths(self.run_dir)

    def test_fresh_start_rejects_a_leftover_stderr_log(self) -> None:
        agent.LOG_FILE.with_name("codex_stderr.log").touch()
        with self.assertRaisesRegex(FileExistsError, "codex_stderr.log"):
            agent.ensure_fresh_run_paths(self.run_dir)

    def test_agent_reads_protected_description_and_uses_absolute_client_path(self) -> None:
        self.run_dir.mkdir()
        (self.run_dir / "environment_description.md").write_text("Wrong description", encoding="utf-8")
        fake_agent = SimpleNamespace(run_dir=self.run_dir, logger=Mock())
        with self.assertRaisesRegex(RuntimeError, "description"):
            agent.SimpleAgent.validate_environment_description(fake_agent)
        self.description.write_text("Authoritative description", encoding="utf-8")
        self.description.chmod(0o444)
        self.assertIsNone(agent.SimpleAgent.validate_environment_description(fake_agent))
        with patch.object(agent, "run_program", return_value="step: 0") as run:
            self.assertEqual(agent.SimpleAgent.run_client(fake_agent, "status"), "step: 0")
        run.assert_called_once_with(f"python3 {self.script} status", cwd=self.run_dir, logger=fake_agent.logger)

    def test_prompts_use_the_mounted_paths(self) -> None:
        main = (AGENT_SRC / "prompts" / "main_prompt.md").read_text(encoding="utf-8")
        continuation = (AGENT_SRC / "prompts" / "continuation.txt").read_text(encoding="utf-8")
        self.assertIn("python3 /client/client.py move", main)
        self.assertIn("`/observations/environment_description.md`", main)
        self.assertIn("`/observations/environment_description.md`", continuation)
        self.assertNotIn("client/observations", main)
        self.assertIn("Do not edit, delete, or otherwise modify files in `/observations/`.", main)
        self.assertIn("# Workspace\n\nYou start in `/home/user/run/agent_run`, which is your workspace.", main)
        self.assertNotIn("{{WORKSPACE_DIR}}", main)

    def test_agent_paths_ignore_environment_overrides(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", (
                "import json, session_inspector as paths; "
                "print(json.dumps([str(paths.CLIENT_SCRIPT_PATH), str(paths.CLIENT_STATE_PATH), "
                "str(paths.ENVIRONMENT_DESCRIPTION_PATH)]))"
            )],
            cwd=AGENT_SRC,
            env=dict(os.environ, ENV_CLIENT_DIR="/wrong-client", ENV_OBSERVATIONS_DIR="/wrong-observations", PYTHONDONTWRITEBYTECODE="1"),
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [
            "/client/client.py", "/client/.client_session.json", "/observations/environment_description.md",
        ])


if __name__ == "__main__":
    unittest.main()
