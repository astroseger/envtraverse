from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_CONFIG = {"model": "openai/gpt-6.1-terra", "reasoning_effort": "max"}


def load_agent_modules(agent_name):
    """Keep this agent's script imports separate from Codex's same-named modules."""
    agent_dir = ROOT / "src/agents" / agent_name / "agent"
    names = ("agent", "agent_funs", "opencode_runner", "session_inspector")
    previous = {name: sys.modules.pop(name) for name in names if name in sys.modules}
    sys.path.insert(0, str(agent_dir))
    try:
        spec = importlib.util.spec_from_file_location("oc_agent_port_subject", agent_dir / "agent.py")
        agent = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(agent)
        sys.modules["agent"] = agent
        spec = importlib.util.spec_from_file_location("oc_supervisor_port_subject", agent_dir / "agent_runner.py")
        supervisor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(supervisor)
        return agent, supervisor
    finally:
        sys.path.pop(0)
        for name in names:
            sys.modules.pop(name, None)
        sys.modules.update(previous)


class OpenCodePreparationTest(unittest.TestCase):
    agent_name = "opencode_agent_v1_08_wstop3"

    def setUp(self):
        self.prepare_run = importlib.import_module(f"src.agents.{self.agent_name}.prepare_run").prepare_run

    def test_defaults_only_add_permissions_and_disable_websearch(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            prepared = self.prepare_run(run, "http://server:8879", "game-alias", {
                **REQUIRED_CONFIG,
                "private-setting": "do-not-expose-this-setting", "games": ["private-game-name"],
            })
            self.assertEqual(prepared.command, ["python3", "agent_runner.py", "game-alias",
                                                "--model", "openai/gpt-6.1-terra", "--reasoning-effort", "max"])
            native = run / "opencode_config/opencode.json"
            self.assertEqual(json.loads(native.read_text()), {
                "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
                "websearch": False,
            })
            config_mount, = [mount for mount in prepared.mounts if mount.source == native]
            self.assertEqual(config_mount.target, "/etc/opencode/opencode.json")
            self.assertTrue(config_mount.read_only)
            self.assertFalse((run / "run/client").exists())
            self.assertFalse((run / "run/codex_runner.py").exists())
            self.assertTrue((run / "run/opencode_runner.py").is_file())
            self.assertEqual(json.loads((run / "client/client_config.json").read_text()), {
                "server_url": "http://server:8879", "observations_path": "/observations",
                "http_read_timeout_seconds": 3900.0,
            })
            for path in run.rglob("*"):
                if path.is_file():
                    self.assertNotIn(b"do-not-expose-this-setting", path.read_bytes())
                    self.assertNotIn(b"private-game-name", path.read_bytes())

    def test_explicit_native_and_harness_options_are_forwarded(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            prepared = self.prepare_run(run, "http://server:8879", "game-alias", {
                **REQUIRED_CONFIG, "opencode_thinking": True,
                "opencode_agent": "build", "opencode_shell_timeout_background": True,
                "supervisor_inactivity_timeout_seconds": 180, "http_read_timeout_seconds": 90,
                "recovery_delay_seconds": 10, "max_recovery_attempts": 2,
            })
            args = prepared.command
            for flag, value in (("--model", "openai/gpt-6.1-terra"), ("--reasoning-effort", "max"),
                                ("--opencode-agent", "build"), ("--recovery-delay-seconds", "10.0"),
                                ("--max-recovery-attempts", "2"),
                                ("--supervisor-inactivity-timeout-seconds", "180.0")):
                self.assertEqual(args[args.index(flag) + 1], value)
            self.assertIn("--opencode-thinking", args)
            native = json.loads((run / "opencode_config/opencode.json").read_text())
            self.assertEqual(native["experimental"], {"shell_timeout_background": True})
            self.assertEqual(json.loads((run / "client/client_config.json").read_text())["http_read_timeout_seconds"], 90)

    def test_model_and_reasoning_effort_are_required_before_creating_files(self):
        for config, missing in (({}, "model"), ({"model": "openai/gpt-6.1-terra"}, "reasoning_effort"),
                                ({"reasoning_effort": "max"}, "model")):
            with self.subTest(config=config), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(RuntimeError, missing):
                    self.prepare_run(Path(directory), "http://server:8879", "game", config)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_invalid_model_and_reasoning_effort_are_rejected_before_creating_files(self):
        for key in ("model", "reasoning_effort"):
            for value in (None, "", " ", "with spaces", "tab\there", "line\nbreak", False, 1, [], {}, "value#variant"):
                with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as directory:
                    with self.assertRaisesRegex(RuntimeError, key):
                        self.prepare_run(Path(directory), "http://server:8879", "game", {**REQUIRED_CONFIG, key: value})
                    self.assertEqual(list(Path(directory).iterdir()), [])

    def test_invalid_provider_and_variant_names_are_rejected_before_creating_files(self):
        for key, value in (("model", "other/model"), ("model", "openai/"), ("reasoning_effort", "max/other")):
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(RuntimeError, key):
                    self.prepare_run(Path(directory), "http://server:8879", "game", {**REQUIRED_CONFIG, key: value})
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_model_prefix_is_forwarded_unchanged_for_runner_normalization(self):
        for model in ("gpt-6.1-terra", "openai/gpt-6.1-terra"):
            with self.subTest(model=model), tempfile.TemporaryDirectory() as directory:
                prepared = self.prepare_run(Path(directory), "http://server:8879", "game",
                                       {**REQUIRED_CONFIG, "model": model})
                self.assertEqual(prepared.command[prepared.command.index("--model") + 1], model)

    def test_invalid_optional_settings_are_rejected_before_creating_files(self):
        cases = (
            {"opencode_thinking": "true"}, {"opencode_agent": ""},
            {"opencode_shell_timeout_background": 1}, {"max_recovery_attempts": True},
            {"recovery_delay_seconds": -1}, {"http_read_timeout_seconds": 0},
        )
        for config in cases:
            with self.subTest(config=config), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(RuntimeError):
                    self.prepare_run(Path(directory), "http://server:8879", "game", {**REQUIRED_CONFIG, **config})
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_unified_controller_discovers_and_prepares_selected_agent(self):
        import run_controller as controller
        from src.controller import run_funs as rf

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = rf.RunPaths(root, backend="opencode_chatgpt_accounts")
            account = paths.accounts_dir / "synthetic_account"
            account.mkdir(parents=True)
            (account / "opencode.db").write_text("synthetic database; never opened")
            config_path = root / "config.yaml"
            config_path.write_text(json.dumps({
                "backend": "opencode_chatgpt_accounts", "agent": self.agent_name,
                "environment": "example", "games": ["counter"], "accounts": [account.name],
                "agent_parameters": REQUIRED_CONFIG,
            }))
            config = controller.parse_run_config(config_path, paths=paths)
            run = root / "game"
            run.mkdir()
            prepared = rf.prepare_agent_run(
                config.agent, run, "http://server:8879", "game-alias", config.agent_parameters,
            )
            self.assertEqual(config.agent, self.agent_name)
            self.assertEqual(prepared.command[:3], ["python3", "agent_runner.py", "game-alias"])
            source = ROOT / "src/agents" / self.agent_name / "agent"
            self.assertEqual((run / "run/agent.py").read_bytes(), (source / "agent.py").read_bytes())
            self.assertTrue((run / "run/opencode_runner.py").is_file())


class OpenCodeBaselinePreparationTest(OpenCodePreparationTest):
    agent_name = "opencode_agent_v1"


class OpenCodeAgentLifecycleTest(unittest.TestCase):
    agent_name = "opencode_agent_v1_08_wstop3"
    reference_agent_name = "agent_v1_08_wstop3"
    reminder_limit = 5
    reminder_includes_continuation = False

    @classmethod
    def setUpClass(cls):
        cls.agent, cls.supervisor = load_agent_modules(cls.agent_name)

    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.workspace = self.directory / "agent_run"
        self.workspace.mkdir()
        self.description = self.directory / "environment_description.md"
        self.description.write_text("Complete the test environment.")
        self.enterContext(patch.multiple(self.agent, LOG_FILE=self.directory / "agent.log",
                                        ENVIRONMENT_DESCRIPTION_PATH=self.description,
                                        SESSION_ID_FILE=self.directory / ".opencode_session_id",
                                        SESSION_ID_TEMP_FILE=self.directory / ".opencode_session_id.tmp"))
        self.enterContext(patch.object(self.agent, "setup_logger", return_value=Mock()))
        self.runner = Mock()
        self.runner_factory = self.enterContext(patch.object(self.agent, "OpenCodeRunner", return_value=self.runner))
        self.subject = self.agent.SimpleAgent(self.workspace, game_name="test-game", model="openai/model",
                                              reasoning_effort="max", thinking=True, opencode_agent="build")
        self.enterContext(patch.object(self.subject, "run_client", return_value="current observation"))
        self.inspection = self.enterContext(patch.object(self.agent, "inspect_sessions"))
        self.inspection.return_value = self.agent.SessionInspection(step_index=0, terminal=False)
        self.git = self.enterContext(patch.object(self.subject, "run_git"))
        self.enterContext(patch.object(self.agent.time, "sleep"))

    def expected_prompts(self):
        directory = ROOT / "src/agents" / self.reference_agent_name / "agent/prompts"

        def read(name):
            return (directory / name).read_text(encoding="utf-8").strip()

        continuation = read("continuation.txt") + "\n" + read("continuation_string.txt")
        reminder = read("stuck_reminder_prompt.txt")
        if self.reminder_includes_continuation:
            reminder = continuation + "\n" + reminder
        initial = (read("main_prompt.md") + "\n\n" + read("continuation_string.txt")
                   + "\n\nThe initial output of the game client:\ncurrent observation")
        return initial, continuation, reminder

    def test_exact_reference_prompts_and_stop_after_ineffective_reminder_budget(self):
        initial, continuation, reminder = self.expected_prompts()
        self.inspection.side_effect = [
            self.agent.SessionInspection(step_index=7, terminal=False)
            for _ in range(self.reminder_limit + 2)
        ]
        self.assertEqual(self.subject.run(), 0)
        self.assertEqual(self.runner.send.call_args_list,
                         [call(initial), call(continuation)] + [call(reminder)] * self.reminder_limit)
        self.assertEqual(self.inspection.call_count, self.reminder_limit + 2)
        self.git.assert_not_called()

    def test_progress_after_reminder_resumes_continuation_then_terminal_stops(self):
        initial, continuation, reminder = self.expected_prompts()
        self.inspection.side_effect = [
            self.agent.SessionInspection(step_index=step, terminal=False) for step in (1, 1, 2, 2)
        ] + [self.agent.SessionInspection(step_index=3, terminal=True)]
        self.assertEqual(self.subject.run(), 0)
        self.assertEqual(self.runner.send.call_args_list,
                         [call(initial), call(continuation), call(reminder), call(continuation), call(reminder)])
        self.git.assert_not_called()

    def test_terminal_after_initial_turn_needs_no_continuation_or_git_snapshot(self):
        initial, _, _ = self.expected_prompts()
        self.inspection.return_value = self.agent.SessionInspection(step_index=1, terminal=True)
        self.assertEqual(self.subject.run(), 0)
        self.runner.send.assert_called_once_with(initial)
        self.git.assert_not_called()

    def test_recovery_starts_new_session_at_step_zero_then_resumes_saved_id(self):
        self.runner.send.side_effect = lambda prompt: self.runner_factory.call_args.kwargs["on_session_started"]("ses_test")
        self.subject.init_recovery()
        self.runner.new_session.assert_called_once_with()
        self.runner.resume_session.assert_not_called()
        self.assertEqual(self.agent.load_session_id(), "ses_test")
        self.assertFalse(self.agent.SESSION_ID_TEMP_FILE.exists())
        initial_prompt = self.runner.send.call_args.args[0]
        self.assertIn(self.subject.main_prompt, initial_prompt)
        self.assertIn("current observation", initial_prompt)
        self.inspection.return_value = self.agent.SessionInspection(step_index=1, terminal=False)
        self.subject.init_recovery()
        self.runner.resume_session.assert_called_once_with("ses_test")
        self.assertEqual(self.runner.send.call_args.args, (self.subject.continuation_prompt(),))

    def test_progressed_game_requires_saved_id_and_terminal_game_does_not_run_model(self):
        self.inspection.return_value = self.agent.SessionInspection(step_index=2, terminal=False)
        with self.assertRaises(FileNotFoundError):
            self.subject.init_recovery()
        self.runner.new_session.assert_not_called()
        self.runner.send.assert_not_called()
        self.inspection.return_value = self.agent.SessionInspection(step_index=2, terminal=True)
        self.subject.init_recovery()
        self.runner.send.assert_not_called()

    def test_runner_receives_selected_options_and_defaults_remain_unspecified(self):
        kwargs = self.runner_factory.call_args.kwargs
        self.assertEqual(kwargs["model"], "openai/model")
        self.assertEqual(kwargs["reasoning_effort"], "max")
        self.assertTrue(kwargs["thinking"])
        self.assertEqual(kwargs["opencode_agent"], "build")
        args = self.agent.parse_args(["--master", "game"])
        self.assertIsNone(args.model)
        self.assertIsNone(args.reasoning_effort)
        self.assertFalse(args.opencode_thinking)
        self.assertIsNone(args.opencode_agent)

    def test_supervisor_forwards_options_to_master_and_recovery(self):
        options = dict(model="openai/model", reasoning_effort="max", thinking=True, opencode_agent="build")
        master = self.supervisor.build_agent_command(game_name="game", recovery=False, **options)
        recovery = self.supervisor.build_agent_command(game_name=None, recovery=True, **options)
        for command in (master, recovery):
            self.assertEqual(command[command.index("--model") + 1], "openai/model")
            self.assertEqual(command[command.index("--reasoning-effort") + 1], "max")
            self.assertEqual(command[command.index("--opencode-agent") + 1], "build")
            self.assertIn("--opencode-thinking", command)
        self.assertIn("--master", master)
        self.assertIn("--recovery", recovery)
        self.assertNotIn("--master", recovery)

    def test_supervisor_stops_active_child_when_interrupted(self):
        process = Mock()
        process.poll.return_value = None
        with patch.object(self.supervisor, "ensure_fresh_runner_paths"), \
                patch.object(self.supervisor.signal, "signal"), \
                patch.object(self.supervisor, "log"), \
                patch.object(self.supervisor, "start_agent", return_value=process), \
                patch.object(self.supervisor, "wait_for_agent", side_effect=KeyboardInterrupt), \
                patch.object(self.supervisor, "terminate_agent") as terminate:
            self.assertEqual(self.supervisor.main(["game"]), 130)
        terminate.assert_called_once_with(process)


class OpenCodeBaselineLifecycleTest(OpenCodeAgentLifecycleTest):
    agent_name = "opencode_agent_v1"
    reference_agent_name = "agent_v1"
    reminder_limit = 1
    reminder_includes_continuation = True


if __name__ == "__main__":
    unittest.main()
