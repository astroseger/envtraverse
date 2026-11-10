"""Offline checks for the first one-prompt Omega experiment adapter."""
from __future__ import annotations

from copy import deepcopy
import importlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import subprocess
import types
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr, redirect_stdout


PACKAGE = Path(__file__).resolve().parents[1]
ROOT = PACKAGE.parents[2]
BASE_CLIENT = ROOT / "src/agents/agent_v1/agent/client"
sys.path.insert(0, str(ROOT))


def source_files(directory: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


class PreparationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("src.agents.omega_agent_v1.prepare_run")

    def prepare(self, directory: Path, settings=None, initial=None):
        config = {"model": "test-model", "reasoning_effort": "medium"}
        if settings is not None:
            config.update(settings)
        return self.module.prepare_run(directory, "http://server:8879", "public-task", config, initial)

    def test_single_prompt_command_and_isolated_persistent_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            result = self.prepare(run)
            self.assertEqual(result.command, [
                "python3", "agent.py", "--master", "public-task", "--model", "test-model",
                "--reasoning-effort", "medium", "--max-new-input-loops", "10000000",
            ])
            self.assertEqual([(item.source, item.target, item.read_only) for item in result.mounts], [
                (run / "client", "/client", False),
                (run / "observations", "/observations", False),
                (run / "run", "/home/user/run", False),
                (run / "omega_memory", "/PeTTa/repos/Omega/memory", False),
            ])
            self.assertEqual(result.environment["CHROMA_DB_PATH"], "/PeTTa/repos/Omega/memory/chroma_db")
            self.assertEqual(result.environment["HF_HUB_OFFLINE"], "1")
            self.assertEqual(result.environment["TRANSFORMERS_OFFLINE"], "1")
            self.assertEqual(result.environment["ANONYMIZED_TELEMETRY"], "False")
            self.assertTrue((run / "omega_memory").is_dir())
            self.assertFalse((run / "run/client").exists())
            self.assertTrue((run / "run/agent.py").is_file())
            self.assertFalse((run / "run/agent_runner.py").exists())
            self.assertFalse((run / "run/codex_runner.py").exists())

    def test_client_sources_remain_byte_for_byte_identical(self):
        self.assertEqual(source_files(BASE_CLIENT), source_files(PACKAGE / "agent/client"))
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            self.prepare(run)
            for relative, content in source_files(BASE_CLIENT).items():
                self.assertEqual((run / "client" / relative).read_bytes(), content, relative)
            self.assertEqual((run / "client/client.py").stat().st_mode & 0o777, 0o444)
            self.assertEqual((run / "observations").stat().st_mode & 0o777, 0o555)

    def test_explicit_cycles_and_client_timeout_are_forwarded(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            result = self.prepare(run, {"max_new_input_loops": 73, "http_read_timeout_seconds": 25.5})
            self.assertEqual(result.command[-2:], ["--max-new-input-loops", "73"])
            config = json.loads((run / "client/client_config.json").read_text())
            self.assertEqual(config, {
                "server_url": "http://server:8879", "observations_path": "/observations",
                "http_read_timeout_seconds": 25.5,
            })

    def test_workspace_seed_is_preserved_and_private_configuration_is_not_exposed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initial = root / "seed"
            initial.mkdir()
            (initial / "instructions.txt").write_text("Public task materials")
            run = root / "prepared"
            run.mkdir()
            config = {"model": "test-model", "reasoning_effort": "medium",
                      "games": ["private-game-id-DO-NOT-COPY"],
                      "secret_setting": "private-config-value-DO-NOT-COPY"}
            original = deepcopy(config)
            self.module.prepare_run(run, "http://server:8879", "public-task", config, initial)
            self.assertEqual((run / "run/workspace_init/instructions.txt").read_text(), "Public task materials")
            for content in source_files(run).values():
                self.assertNotIn(b"private-game-id-DO-NOT-COPY", content)
                self.assertNotIn(b"private-config-value-DO-NOT-COPY", content)
            self.assertEqual(config, original)

    def test_invalid_parameters_fail_before_creating_run_files(self):
        invalid = [
            {name: value}
            for name in ("model", "reasoning_effort")
            for value in (None, "", 42, [])
        ]
        invalid += [{"max_new_input_loops": value} for value in (0, -1, True, 1.5, "1000")]
        invalid += [{"http_read_timeout_seconds": value} for value in (0, -1, True, float("nan"), float("inf"))]
        for settings in invalid:
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as temporary:
                run = Path(temporary)
                with self.assertRaises((RuntimeError, ValueError)):
                    self.prepare(run, settings)
                self.assertEqual(list(run.iterdir()), [])


    def test_invalid_server_url_fails_before_preparation(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            with self.assertRaises((RuntimeError, ValueError)):
                self.module.prepare_run(run, "invalid server", "public-task",
                                        {"model": "test-model", "reasoning_effort": "medium"})
            self.assertEqual(list(run.iterdir()), [])


class ExecRequested(BaseException):
    pass


class LaunchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("src.agents.omega_agent_v1.launch")

    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.account = self.directory / "account with spaces:local"
        self.account.mkdir(mode=0o700)
        self.credential = self.account / "chatgpt.json"
        self.credential.write_text("These are dummy credentials that must never be read")
        self.credential.chmod(0o600)
        self.run = self.directory / "new run"
        self.arguments = ["--server-url", "http://server:8879", "--game", "public-task",
                          "--account", str(self.account), "--run-dir", str(self.run),
                          "--model", "test-model"]

    def test_launch_replaces_itself_with_docker_and_writable_account_mount(self):
        with patch.object(self.module.subprocess, "check_output", return_value="omega-test-image:uid-gid\n") as build, \
             patch.object(self.module.os, "execvp", side_effect=ExecRequested) as execute, \
             redirect_stderr(io.StringIO()), self.assertRaises(ExecRequested):
            self.module.main(self.arguments)
        build.assert_called_once()
        command = execute.call_args.args[1]
        self.assertEqual(command[:8], ["docker", "run", "--rm", "--pull=never", "--init", "--network", "host", "--mount"])
        self.assertIn("type=bind,source=" + str(self.account) + ",target=/home/user/.omega_auth", command)
        self.assertIn("CHROMA_DB_PATH=/PeTTa/repos/Omega/memory/chroma_db", command)
        self.assertEqual(command[command.index("omega-test-image:uid-gid") + 1:], [
            "python3", "agent.py", "--master", "public-task", "--model", "test-model",
            "--reasoning-effort", "medium", "--max-new-input-loops", "10000000",
        ])
        self.assertNotIn("agent_runner.py", command)
        self.assertTrue((self.run / "client/client_config.json").exists())
        self.assertEqual(self.credential.read_text(), "These are dummy credentials that must never be read")

    def test_existing_output_and_bad_auth_permissions_fail_before_build(self):
        cases = ("existing-output", "public-credentials")
        for case in cases:
            with self.subTest(case=case):
                if case == "existing-output":
                    self.run.mkdir()
                else:
                    self.run.rmdir()
                    self.credential.chmod(0o644)
                with patch.object(self.module.subprocess, "check_output") as build, \
                     patch.object(self.module.os, "execvp") as execute, redirect_stderr(io.StringIO()):
                    self.assertEqual(self.module.main(self.arguments), 1)
                build.assert_not_called()
                execute.assert_not_called()

    def test_invalid_model_or_reasoning_fails_before_build(self):
        for option in ("--model", "--reasoning-effort"):
            with self.subTest(option=option), patch.object(self.module.subprocess, "check_output") as build, \
                 redirect_stderr(io.StringIO()):
                self.assertEqual(self.module.main([*self.arguments, option, " "]), 1)
                build.assert_not_called()
                self.assertFalse(self.run.exists())


class RuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = importlib.import_module("src.agents.omega_agent_v1.agent.agent")

    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.directory / "run"
        self.root.mkdir()
        self.workspace = self.root / "agent_run"
        self.petta = self.directory / "PeTTa"
        self.omega = self.petta / "repos/Omega"
        self.omega.mkdir(parents=True)
        (self.omega / "lib_omega.metta").write_text("; fake runtime entry")
        (self.petta / "run.sh").write_text("# fake runtime launcher")
        self.template = self.directory / "memory-template"
        self.template.mkdir()
        (self.template / "prompt.txt").write_text("Unchanged Omega prompt")
        self.client = self.directory / "client"
        self.client.mkdir()
        (self.client / "client.py").write_text("# fake unchanged-client boundary")
        self.observations = self.directory / "observations"
        self.observations.mkdir()
        self.description = self.observations / "environment_description.md"
        self.prompts = self.root / "prompts"
        self.prompts.mkdir()
        (self.prompts / "main_prompt.md").write_text("Read the environment goal and solve it.")
        (self.prompts / "continuation_string.txt").write_text("Continue until completed.")
        (self.root / "run_task.metta").write_text("; local channel adapter")
        (self.root / "prompt_channel.py").write_text("# local channel adapter")
        for name, value in {
            "ROOT": self.root, "DEFAULT_AGENT_RUN_DIR": self.workspace,
            "WORKSPACE_INIT_DIR": self.root / "workspace_init", "PROMPT_DIR": self.prompts,
            "CLIENT_SCRIPT_PATH": self.client / "client.py",
            "CLIENT_STATE_PATH": self.client / ".client_session.json",
            "OBSERVATIONS_DIR": self.observations, "ENVIRONMENT_DESCRIPTION_PATH": self.description,
            "PETTA_ROOT": self.petta, "OMEGA_ROOT": self.omega, "MEMORY_TEMPLATE_DIR": self.template,
        }.items():
            self.enterContext(patch.object(self.module, name, value))
        self.enterContext(patch.dict(self.module.os.environ, {
            "OMEGA_CHATGPT_AUTH_FILE": str(self.directory / "account/chatgpt.json"),
        }))
        self.enterContext(patch.object(self.module.Path, "home", return_value=self.directory / "home"))

    def test_start_initializes_client_once_then_execs_omega_in_task_workspace(self):
        def initialize(command, **kwargs):
            self.description.write_text("The environment goal")
            return types.SimpleNamespace(stdout="Initial observation", stderr="")

        with patch.object(self.module.subprocess, "run", side_effect=initialize) as client, \
             patch.object(self.module.os, "chdir") as chdir, \
             patch.object(self.module.os, "execvpe", side_effect=ExecRequested) as execute, \
             redirect_stdout(io.StringIO()), self.assertRaises(ExecRequested):
            self.module.main(["--master", "public-task", "--model", "chosen-model", "--reasoning-effort", "high"])
        client.assert_called_once()
        self.assertEqual(client.call_args.args[0], [sys.executable, str(self.client / "client.py"), "start", "public-task"])
        self.assertEqual(client.call_args.kwargs["cwd"], self.workspace)
        chdir.assert_called_once_with(self.workspace)
        command = execute.call_args.args[1]
        self.assertEqual(command[:3], ["/bin/sh", str(self.petta / "run.sh"), str(self.root / "run_task.metta")])
        for option in ("provider=ChatGPT", "commchannel=envtraverse", "model=chosen-model",
                       "reasoningMode=high", "maxNewInputLoops=10000000", "maxWakeLoops=0"):
            self.assertIn(option, command)
        self.assertEqual(execute.call_args.args[2]["CHROMA_DB_PATH"], str(self.omega / "memory/chroma_db"))
        prompt = (self.root / "task_prompt.txt").read_text()
        for part in ("Read the environment goal and solve it.", "Continue until completed.", "Initial observation"):
            self.assertEqual(prompt.count(part), 1)
        self.assertTrue((self.workspace / "repos").is_symlink())
        self.assertEqual((self.workspace / "repos").resolve(), self.petta / "repos")
        self.assertEqual((self.omega / "memory/prompt.txt").read_text(), "Unchanged Omega prompt")
        policy = json.loads((self.root / "omega-policy.json").read_text())
        writable = policy["filesystem_policy"]["read_write"]
        self.assertIn("/client", writable)
        self.assertIn("/observations", writable)
        self.assertIn(str(self.directory / "account"), writable)

    def test_client_failure_is_not_retried_and_omega_is_not_started(self):
        failure = subprocess.CalledProcessError(5, ["client"], stderr="Client failed")
        with patch.object(self.module.subprocess, "run", side_effect=failure) as client, \
             patch.object(self.module.os, "execvpe") as execute, redirect_stderr(io.StringIO()):
            self.assertEqual(self.module.main(["--master", "public-task"]), 1)
        client.assert_called_once()
        execute.assert_not_called()
        self.assertFalse((self.root / "task_prompt.txt").exists())

    def test_missing_description_prevents_model_start(self):
        with patch.object(self.module.subprocess, "run", return_value=types.SimpleNamespace(stdout="Initial", stderr="")), \
             patch.object(self.module.os, "execvpe") as execute, redirect_stderr(io.StringIO()):
            self.assertEqual(self.module.main(["--master", "public-task"]), 1)
        execute.assert_not_called()

    def test_existing_memory_is_preserved_and_rejected(self):
        memory = self.omega / "memory"
        memory.mkdir()
        history = memory / "history.metta"
        history.write_text("Previous task history")
        with self.assertRaises(FileExistsError):
            self.module.initialize_omega_memory()
        self.assertEqual(history.read_text(), "Previous task history")

    def test_reserved_workspace_import_path_is_not_overwritten(self):
        reserved = self.root / "workspace_init/repos"
        reserved.mkdir(parents=True)
        (reserved / "task-data").write_text("Keep this file")
        with self.assertRaises(FileExistsError):
            self.module.prepare_agent_run(self.workspace)
        self.assertFalse(self.workspace.exists())
        self.assertEqual((reserved / "task-data").read_text(), "Keep this file")


class PromptChannelTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(sys, "path", list(sys.path)))
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.prompt_path = self.directory / "task_prompt.txt"
        self.prompt_path.write_text("One task prompt")
        self.registered = {}
        channels = types.ModuleType("channels")
        channels.CommChannel = object
        channels.registerCommChannel = self.registered.__setitem__
        config = types.ModuleType("config")
        config.config_get_by_key = lambda name: str(self.prompt_path) if name == "taskPromptPath" else None
        self.enterContext(patch.dict(sys.modules, {"channels": channels, "config": config}))
        spec = importlib.util.spec_from_file_location("omega_test_prompt_channel", PACKAGE / "agent/prompt_channel.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_plugin_delivers_prompt_once_and_never_generates_continuations(self):
        self.module.loadOmegaPlugin()
        channel = self.registered["envtraverse"]
        channel.start()
        self.assertEqual(channel.receive(), "One task prompt")
        for _ in range(5):
            self.assertEqual(channel.receive(), "")
        channel.stop()
        self.assertEqual(channel.receive(), "")

    def test_channel_output_is_a_structured_message(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.module.PromptChannel().send("Progress: Unicode ✓")
        self.assertEqual(json.loads(output.getvalue()), {"type": "omega_message", "message": "Progress: Unicode ✓"})

    def test_empty_task_is_rejected(self):
        self.prompt_path.write_text(" ")
        with self.assertRaises(RuntimeError):
            self.module.PromptChannel().start()


if __name__ == "__main__":
    unittest.main()
