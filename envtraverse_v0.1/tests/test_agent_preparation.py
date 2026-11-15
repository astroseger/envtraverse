from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_controller as controller
import run_backends as rb
import run_funs as rf
import src.agents
from src.agents.agent_v1.prepare_run import prepare_run


BACKENDS = (rb.CODEX_CHATGPT, rb.CODEX_API_KEY)


class AgentPreparationTest(unittest.TestCase):
    def test_agent_v1_uses_configured_settings_and_does_not_expose_the_run_config(self) -> None:
        for settings, model, effort in (
            ({"model": "gpt-5.5", "reasoning_effort": "medium"}, "gpt-5.5", "medium"),
            ({"model": "gpt-5.6-sol", "reasoning_effort": "max"}, "gpt-5.6-sol", "max"),
        ):
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as directory:
                run_dir = Path(directory)
                config = {"agent": "agent_v1", "games": ["private-task-id-for-test"],
                          "agent_parameters": {
                              "custom_setting": {"value": "do-not-expose-raw-run-config"}, **settings,
                          }}
                original = deepcopy(config)
                result = prepare_run(run_dir, "http://env-server:19234", "public-alias", config["agent_parameters"])
                self.assertEqual(result.command, [
                    "python3", "agent_runner.py", "public-alias", "--model", model,
                    "--reasoning-effort", effort,
                ])
                self.assertEqual(result.environment, {})
                self.assertEqual([(m.source, m.target, m.read_only) for m in result.mounts], [
                    (run_dir / "client", "/client", False),
                    (run_dir / "observations", "/observations", False),
                    (run_dir / "run", "/home/user/run", False),
                ])
                self.assertEqual(json.loads((run_dir / "client/client_config.json").read_text()), {
                    "server_url": "http://env-server:19234", "observations_path": "/observations",
                    "http_read_timeout_seconds": 3900.0,
                })
                self.assertFalse((run_dir / "run/client").exists())
                self.assertFalse((run_dir / "run/workspace_init").exists())
                for path in run_dir.rglob("*"):
                    if path.is_file():
                        for private in (b"private-task-id-for-test", b"do-not-expose-raw-run-config"):
                            self.assertNotIn(private, path.read_bytes(), str(path))
                self.assertEqual(config, original)

    def test_agent_v1_rejects_invalid_settings_before_creating_files(self) -> None:
        for key in ("model", "reasoning_effort"):
            for value in (None, "", 42, [], {}):
                with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as directory:
                    config = {"model": "gpt-5.6-sol", "reasoning_effort": "max", key: value}
                    with self.assertRaisesRegex(RuntimeError, key):
                        prepare_run(Path(directory), "http://server:8879", "alias", config)
                    self.assertEqual(list(Path(directory).iterdir()), [])

    def test_agent_v1_requires_both_settings_before_creating_files(self) -> None:
        for config, missing in (
            ({}, "model"),
            ({"reasoning_effort": "max"}, "model"),
            ({"model": "gpt-5.6-sol"}, "reasoning_effort"),
        ):
            with self.subTest(config=config), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(RuntimeError, f"{missing} is required"):
                    prepare_run(Path(directory), "http://server:8879", "alias", config)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_both_controllers_require_an_existing_agent_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "codex_accounts/codex01").mkdir(parents=True)
            config_path = root / "run.yaml"
            for settings in ({}, *({"agent": name} for name in
                                  (None, "", 42, [], "missing", "../agent_v1", "agent_v1.prepare_run", "9bad"))):
                for backend_name in BACKENDS:
                    config_path.write_text(yaml.safe_dump({
                        "backend": backend_name, "environment": "example", "games": ["counter"],
                        "accounts": ["codex01"] if backend_name == rb.CODEX_CHATGPT else [],
                        "agent_parameters": {}, **settings,
                    }))
                    with self.subTest(backend=backend_name, settings=settings), \
                            self.assertRaisesRegex(RuntimeError, "agent"):
                        controller.parse_run_config(config_path, paths=rf.RunPaths(root))

    def test_both_controllers_use_selected_hooks_without_assuming_an_agent_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            package = root / "agents/custom_agent"
            package.mkdir(parents=True)
            (package / "Dockerfile").write_text("FROM scratch\nWORKDIR /task\n")
            (package / "prepare_run.py").write_text('''\
from agent_api import AgentMount, LaunchPreparation

def prepare_run(run_dir, server_url, game_alias, parsed_run_config, initial_files_dir=None):
    assert set(parsed_run_config) == {"model", "custom_value"}
    assert parsed_run_config["model"] == {"custom": "format"}
    return LaunchPreparation(
        command=["custom-program", game_alias, parsed_run_config["custom_value"]],
        mounts=[AgentMount(run_dir, "/task", read_only=True)],
        environment={"TASK_URL": server_url, "INITIAL_FILES": str(initial_files_dir)},
    )
''')
            stack.enter_context(patch.object(rf, "AGENTS_DIR", package.parent))
            stack.enter_context(patch.object(src.agents, "__path__", [str(package.parent)]))
            stack.enter_context(patch.object(rf, "SERVER_PORT", 19234))
            accounts = root / "codex_accounts"
            (accounts / "codex01").mkdir(parents=True)
            stack.enter_context(patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"}))
            stack.enter_context(patch.object(rb, "clean_codex_account"))
            stack.enter_context(patch.object(rf, "log_info"))
            for name in ("src.agents.custom_agent", "src.agents.custom_agent.prepare_run"):
                stack.callback(sys.modules.pop, name, None)
            importlib.invalidate_caches()
            settings = {
                "agent": "custom_agent", "environment": "arc3", "games": ["private-game"],
                "agent_parameters": {"custom_value": "kept", "model": {"custom": "format"}},
            }
            config_path = root / "config.yaml"
            images = rf.RunImages.create()
            names = rf.build_container_names(settings["games"])

            def schedule(_environment, _names, _mapping, *, slots, launch_agent, images, paths, finalize_game):
                return launch_agent(slots[0], "public-alias", "private-game", lambda: None).process.poll()

            for backend_name in BACKENDS:
                run_settings = dict(settings, backend=backend_name,
                                    accounts=["codex01"] if backend_name == rb.CODEX_CHATGPT else [])
                config_path.write_text(yaml.safe_dump(run_settings))
                with self.subTest(backend=backend_name), \
                        patch.object(rf, "run_scheduler", side_effect=schedule), \
                        patch.object(controller.subprocess, "Popen", return_value=Mock(poll=Mock(return_value=0))) as popen:
                    config = controller.parse_run_config(config_path, paths=rf.RunPaths(root))
                    self.assertEqual(config.parsed_run_config, run_settings)
                    self.assertEqual(config.agent_parameters, settings["agent_parameters"])
                    backend = rb.create_backend(config)
                    backend.account_locks = {"codex01": 123}
                    self.assertEqual(controller.run_scheduler(
                        config, names, {"public-alias": "private-game"}, images=images, backend=backend,
                    ), 0)
                    command = popen.call_args.args[0]
                    self.assertEqual(command[command.index(images.agent) + 1:], ["custom-program", "public-alias", "kept"])
                    self.assertIn(f"{root / 'run/public-alias'}:/task:ro", command)
                    self.assertIn(f"TASK_URL=http://{names.servers['private-game']}:19234", command)
                    self.assertIn(f"INITIAL_FILES={rf.initial_workspace_files('arc3')}", command)
                    self.assertNotIn("/client:rw", " ".join(command))
                    self.assertNotIn("agent_runner.py", command)
                    self.assertNotIn("--model", command)
                    self.assertIn("--pull=never", command)
                    self.assertEqual(command[command.index("--network") + 1], names.internal_network)
                    if backend_name == rb.CODEX_CHATGPT:
                        self.assertIn(f"{accounts / 'codex01'}:/home/user/.codex", command)
                    else:
                        self.assertIn("CODEX_API_KEY", command)
                        self.assertNotIn("test-only", repr(command))
                        self.assertEqual(popen.call_args.kwargs["env"]["CODEX_API_KEY"], "test-only")


if __name__ == "__main__":
    unittest.main()
