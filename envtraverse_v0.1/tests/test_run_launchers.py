from __future__ import annotations

from contextlib import ExitStack, chdir, contextmanager, nullcontext
from dataclasses import replace
import io
import json
import logging
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_controller as controller
from src.controller import run_backends as rb
from src.account_locks import account_lock
from src.controller import run_funs as rf
from src.agent_api import AgentMount

BACKENDS = (rb.CODEX_CHATGPT, rb.CODEX_API_KEY)
IMAGES = rf.RunImages.create()


def make_config(backend_name, *, variables=(), paths=None):
    paths = rf.RunPaths() if paths is None else paths
    settings = dict(
        games=["counter"], paths=replace(paths, backend=backend_name), backend=backend_name,
        environment=rf.EnvironmentConfig("example", {"target": 2}, list(variables)),
        agent="agent_v1",
        parsed_run_config={"agent": "agent_v1", "agent_parameters": {
            "model": "test-model", "reasoning_effort": "medium",
        }},
    )
    if backend_name == rb.CODEX_CHATGPT:
        settings["accounts"] = ["codex01"]
    return controller.RunConfig(**settings)


def make_backend(config):
    backend = rb.create_backend(config)
    # Launch tests mock account helpers. Reservation itself is covered by the
    # account-lock tests, and no unit test may inspect real Docker containers.
    backend.account_locks = {account: 123 for account in config.accounts}
    return backend


def example_preparation(alias="alias", model="test-model", effort="medium", *, paths=None):
    paths = rf.RunPaths() if paths is None else paths
    game_dir = paths.run_dir / alias
    return rf.LaunchPreparation(
        command=["python3", "agent_runner.py", alias, "--model", model, "--reasoning-effort", effort],
        mounts=[AgentMount(game_dir / "client", "/client"),
                AgentMount(game_dir / "observations", "/observations"),
                AgentMount(game_dir / "run", "/home/user/run")],
    )


def prepare_game(alias, environment, *, paths=None):
    paths = rf.RunPaths() if paths is None else paths
    paths.run_dir.mkdir(exist_ok=True)
    game_dir = rf.prepare_game_directory(alias, paths=paths)
    rf.prepare_agent_run(
        "agent_v1", game_dir, f"http://env-server:{rf.SERVER_PORT}", alias,
        {"model": "gpt-5.6-sol", "reasoning_effort": "max"},
        initial_files_dir=rf.initial_workspace_files(environment),
    )
    return game_dir


def environment_arguments(command):
    return [command[index + 1] for index, part in enumerate(command) if part == "-e"]


class LauncherTest(unittest.TestCase):
    def test_backend_runs_keep_outputs_in_explicit_or_invocation_root(self) -> None:
        for backend_name, mode in ((backend, mode) for backend in BACKENDS
                                   for mode in ("explicit_root", "cwd", "external_config")):
            with self.subTest(backend=backend_name, mode=mode), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                paths = rf.RunPaths(Path(directory) / "experiment with spaces", backend=backend_name)
                (paths.accounts_dir / "codex01").mkdir(parents=True)
                (paths.root / "resources").mkdir()
                selected_config = (Path(directory) / "selected.yaml") if mode == "external_config" else paths.run_config
                selected_config.write_text(
                    f"backend: {backend_name}\n"
                    "agent: agent_v1\nenvironment: example\ngames: [counter]\naccounts: [codex01]\n"
                    "agent_parameters:\n  model: test-model\n  reasoning_effort: medium\n"
                    "server_mounts: [{source: resources, target: /resources}]\n"
                )
                logger = logging.Logger("run-root-test")
                stack.enter_context(patch.object(rf, "LOGGER", logger))
                stack.enter_context(patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}))
                for name in ("verify_container_names_available", "verify_network_names_available", "build_dockers", "remove_run_images"):
                    stack.enter_context(patch.object(rf, name))
                stack.enter_context(patch.object(rf, "image_exists", return_value=False))
                preflight = "verify_codex_accounts" if backend_name == rb.CODEX_CHATGPT else "verify_codex_api_key"
                stack.enter_context(patch.object(rb, preflight))
                stack.enter_context(patch.object(rb.RunBackend, "reserve", return_value=nullcontext()))
                if backend_name == rb.CODEX_API_KEY:
                    stack.enter_context(patch.object(rb, "confirm_api_key_run"))
                stack.enter_context(patch.object(rf.subprocess, "run", side_effect=AssertionError("Unexpected subprocess")))
                stack.enter_context(patch.object(rf.subprocess, "Popen", side_effect=AssertionError("Unexpected subprocess")))

                def schedule(config, _names, _mapping, *, images, backend):
                    self.assertEqual(config.paths, paths)
                    self.assertEqual(config.environment.mounts,
                                     (rf.ServerMount(paths.root / "resources", "/resources"),))
                    game = rf.prepare_game_directory("alias", paths=config.paths)
                    rf.prepare_agent_run(config.agent, game, "http://server:8879", "alias", config.agent_parameters)
                    self.assertEqual(json.loads((paths.run_dir / "docker_images.json").read_text())["agent"], images.agent)
                    return 0

                stack.enter_context(patch.object(controller, "run_scheduler", side_effect=schedule))
                if mode == "explicit_root":
                    result = controller.main(["--run-root", str(paths.root)])
                else:
                    with chdir(paths.root):
                        arguments = ["--run-config", str(selected_config)] if mode == "external_config" else []
                        result = controller.main(arguments)
                for handler in logger.handlers:
                    stack.callback(handler.close)
                self.assertEqual(result, 0)
                self.assertIn(str(paths.root), paths.runner_log.read_text())
                self.assertTrue((paths.run_dir / "alias/client/client_config.json").is_file())
                self.assertTrue((paths.run_dir / "alias/run/agent.py").is_file())
                self.assertFalse(paths.run_tb_dir.exists())

    def test_account_preflight_runs_source_script_with_external_account_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = rf.RunPaths(Path(directory))
            config = make_config(rb.CODEX_CHATGPT, paths=paths)
            with patch.object(rb, "run_reserved_live_command") as run:
                rb.verify_codex_accounts(config, {"codex01": 123})
            run.assert_called_once_with(
                ["bash", str(ROOT / "codex_accounts/check_account.sh"),
                 str(paths.accounts_dir / "codex01"), "--lock-fd", "123"], 123,
            )

    def test_server_and_client_configuration_use_the_same_nondefault_port(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(rf, "SERVER_PORT", 19234):
            for index, backend_name in enumerate(BACKENDS):
                game_dir = prepare_game(str(index), "example", paths=rf.RunPaths(Path(directory)))
                config = json.loads((game_dir / "client" / "client_config.json").read_text(encoding="utf-8"))
                self.assertEqual(config["server_url"], "http://env-server:19234")
            command = rf.docker_server_command("env-server", "internet", "internal", rf.EnvironmentConfig("example", {}, []), "alias", "counter", image=IMAGES.server)
            self.assertIn("ENV_SERVER_PORT=19234", environment_arguments(command))

    def test_server_command_forwards_only_names_and_preserves_parameters(self) -> None:
        environment = rf.EnvironmentConfig(
            "arc3", {"competition": False, "tag": "api-test", "nested": {"values": [None, 2]}},
            ["ARC_API_KEY", "CUSTOM_SETTING"],
        )
        with patch.dict(os.environ, {"ARC_API_KEY": "private-key", "CUSTOM_SETTING": "private-setting"}):
            command = rf.docker_server_command("server", "internet", "internal", environment, "public-alias", "ls20", image=IMAGES.server)
        self.assertEqual(command[command.index("--environment") + 1], "arc3")
        self.assertEqual(json.loads(command[command.index("--parameters") + 1]), environment.parameters)
        forwarded = environment_arguments(command)
        self.assertIn("ARC_API_KEY", forwarded)
        self.assertIn("CUSTOM_SETTING", forwarded)
        self.assertEqual(command[command.index("--game-id") + 1], "ls20")
        self.assertEqual(command[command.index("--game-alias") + 1], "public-alias")
        self.assertFalse(any("GAME_ID_MAPPING_JSON" in value for value in forwarded))
        self.assertIn(f"{rf.RunPaths().run_dir / 'public-alias' / 'server'}:/data", command)
        self.assertEqual([command[i + 1] for i, part in enumerate(command) if part == "--network"], ["internet", "internal"])
        self.assertNotIn("-p", command)
        self.assertNotIn("private-key", repr(command))
        self.assertNotIn("private-setting", repr(command))
        self.assertNotIn("OPENAI_API_KEY", repr(command))
        self.assertLess(command.index("ARC_API_KEY"), command.index(IMAGES.server))
        self.assertIn("--pull=never", command)
        self.assertNotIn("--inactivity-timeout-seconds", command)
        self.assertNotIn("--terminal-grace-seconds", command)

    def test_server_launch_forwards_timeouts_separately_from_environment_parameters(self) -> None:
        environment = rf.EnvironmentConfig("example", {"target": 2}, [])
        for inactivity, grace in ((1800, 600), (None, 600), (1800, None)):
            with (
                self.subTest(inactivity=inactivity, grace=grace),
                patch.object(rf.subprocess, "Popen") as popen,
                patch.object(rf, "log_info"),
            ):
                rf.start_server(
                    "server", "internet", "internal", environment, "alias", "counter", image=IMAGES.server,
                    server_inactivity_timeout_seconds=inactivity, server_terminal_grace_seconds=grace,
                )
            command = popen.call_args.args[0]
            for flag, value in (("--inactivity-timeout-seconds", inactivity), ("--terminal-grace-seconds", grace)):
                if value is None:
                    self.assertNotIn(flag, command)
                else:
                    self.assertEqual(command[command.index(flag) + 1], str(value))
            self.assertEqual(json.loads(command[command.index("--parameters") + 1]), {"target": 2})

    def test_terminal_bench_mount_is_server_only_and_ids_are_per_game(self) -> None:
        source = ROOT / "run_tb/socket"
        environment = rf.EnvironmentConfig("terminal_bench", {}, [],
                                           (rf.ServerMount(source, "/run/terminal-bench"),))
        for alias in ("1" * 32, "2" * 32):
            command = rf.docker_server_command("server", "internet", "internal", environment,
                                               alias, "terminal-bench-2.1-fix-git", image=IMAGES.server)
            mount = command[command.index("--mount") + 1]
            self.assertEqual(mount, f"type=bind,src={source},dst=/run/terminal-bench")
            self.assertLess(command.index("--mount"), command.index(IMAGES.server))
            self.assertEqual(json.loads(command[command.index("--parameters") + 1]), {"session_id": alias})
        self.assertEqual(environment.parameters, {})
        commands = [rf.docker_proxy_command("proxy", "internet", "internal", image=IMAGES.proxy)]
        commands.append(controller.docker_agent_command(
            "codex01", "agent", "internal", "server", "proxy", example_preparation(), agent_image=IMAGES.agent,
            backend=make_backend(make_config(rb.CODEX_CHATGPT)),
        ))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"}):
            commands.append(controller.docker_agent_command(
                "api-worker-0", "agent", "internal", "server", "proxy", example_preparation(),
                agent_image=IMAGES.agent, backend=make_backend(make_config(rb.CODEX_API_KEY)),
            ))
        for command in commands:
            self.assertNotIn(str(source), repr(command))
            self.assertNotIn("/run/terminal-bench", repr(command))

    def test_proxy_joins_both_shared_networks_at_launch(self) -> None:
        command = rf.docker_proxy_command("proxy", "internet", "internal", image=IMAGES.proxy)
        self.assertEqual([command[i + 1] for i, part in enumerate(command) if part == "--network"], ["internet", "internal"])
        self.assertEqual(command[-1], IMAGES.proxy)
        self.assertNotIn("-p", command)

    def test_example_command_has_no_arc_specific_settings_even_when_key_is_exported(self) -> None:
        with patch.dict(os.environ, {"ARC_API_KEY": "unused-secret"}):
            command = rf.docker_server_command(
                "server", "internet", "internal", rf.EnvironmentConfig("example", {"target": 2}, []), "alias", "counter", image=IMAGES.server,
            )
        self.assertNotIn("ARC_API_KEY", repr(command))
        self.assertNotIn("unused-secret", repr(command))
        self.assertEqual(json.loads(command[command.index("--parameters") + 1]), {"target": 2})

    def test_server_logs_do_not_contain_forwarded_values(self) -> None:
        environment = rf.EnvironmentConfig("example", {}, ["ENVIRONMENT_API_KEY"])
        with (
            patch.dict(os.environ, {"ENVIRONMENT_API_KEY": "sensitive-value"}),
            patch.object(rf.subprocess, "Popen") as popen,
            patch("sys.stdout", new_callable=io.StringIO) as output,
            self.assertLogs(rf.LOGGER, level="INFO") as logs,
        ):
            rf.start_server("server", "internet", "internal", environment, "alias", "counter", image=IMAGES.server)
        self.assertNotIn("sensitive-value", "\n".join(logs.output) + output.getvalue() + repr(popen.call_args))
        self.assertIn("ENVIRONMENT_API_KEY", "\n".join(logs.output))

    def test_server_refuses_missing_variables_before_starting_process(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch.object(rf.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "ENVIRONMENT_API_KEY"):
                rf.start_server(
                    "server", "internet", "internal", rf.EnvironmentConfig("example", {}, ["ENVIRONMENT_API_KEY"]),
                    "alias", "counter",
                    image=IMAGES.server,
                )
            popen.assert_not_called()

    def test_both_controllers_fail_missing_variables_before_docker_or_workspace_changes(self) -> None:
        for backend_name in BACKENDS:
            with self.subTest(backend=backend_name), ExitStack() as stack:
                config = make_config(backend_name, variables=["ENVIRONMENT_API_KEY"])
                stack.enter_context(patch.dict(os.environ, {"OPENAI_API_KEY": "agent-only-key"}, clear=True))
                stack.enter_context(patch.object(controller, "parse_run_config", return_value=config))
                stack.enter_context(patch.object(rb.RunBackend, "reserve", return_value=nullcontext()))
                if backend_name == rb.CODEX_API_KEY:
                    stack.enter_context(patch.object(rb, "confirm_api_key_run"))
                checks = [
                    stack.enter_context(patch.object(rf, method))
                    for method in (
                        "verify_container_names_available", "verify_network_names_available",
                        "prepare_run_root", "setup_runner_logging", "build_dockers", "make_run_writable",
                    )
                ]
                stack.enter_context(patch.object(rf, "LOGGER"))
                error_output = stack.enter_context(patch("sys.stderr", new_callable=io.StringIO))
                self.assertEqual(controller.main([]), 1)
                self.assertIn("ENVIRONMENT_API_KEY", error_output.getvalue())
                self.assertNotIn("agent-only-key", error_output.getvalue())
                for check in checks:
                    check.assert_not_called()

    def test_both_controllers_can_launch_example_without_arc_credentials(self) -> None:
        for backend_name in BACKENDS:
            with self.subTest(backend=backend_name), ExitStack() as stack:
                config = make_config(backend_name)
                stack.enter_context(patch.dict(os.environ, {"OPENAI_API_KEY": "agent-only-key"}, clear=True))
                stack.enter_context(patch.object(controller, "parse_run_config", return_value=config))
                stack.enter_context(patch.object(rb.RunBackend, "reserve", return_value=nullcontext()))
                for method in (
                    "verify_container_names_available", "verify_network_names_available",
                    "prepare_run_root", "setup_runner_logging", "make_run_writable",
                ):
                    stack.enter_context(patch.object(rf, method))
                build = stack.enter_context(patch.object(rf, "built_run_images"))
                build.return_value.__enter__.return_value = IMAGES
                stack.enter_context(patch.object(rf, "LOGGER"))
                scheduler = stack.enter_context(patch.object(controller, "run_scheduler", return_value=0))
                if backend_name == rb.CODEX_API_KEY:
                    confirmation = stack.enter_context(patch.object(rb, "confirm_api_key_run"))
                    stack.enter_context(patch.object(rb, "verify_codex_api_key"))
                else:
                    stack.enter_context(patch.object(rb, "verify_codex_accounts"))
                self.assertEqual(controller.main([]), 0)
                build.assert_called_once_with("example", "agent_v1", agent_docker_build_args={}, paths=config.paths)
                self.assertEqual(scheduler.call_args.kwargs["images"], IMAGES)
                self.assertEqual(scheduler.call_args.kwargs["backend"].config, config)
                self.assertIs(scheduler.call_args.args[0], config)
                if backend_name == rb.CODEX_API_KEY:
                    confirmation.assert_called_once_with()

    def test_all_backends_forward_server_timeouts_and_prepare_workspace(self) -> None:
        for backend_name in (*BACKENDS, rb.OPENCODE_CHATGPT):
            with self.subTest(backend=backend_name), ExitStack() as stack:
                config = replace(make_config(backend_name), server_inactivity_timeout_seconds=1800,
                                 server_terminal_grace_seconds=600)
                if backend_name == rb.OPENCODE_CHATGPT:
                    config = replace(config, agent="opencode_agent_v1", accounts=["opencode01"])
                backend = make_backend(config)
                names = rf.build_container_names(config.games)
                mapping = {"public-alias": "counter"}
                for method in ("create_network", "remove_network", "stop_all", "stop_container", "log_info"):
                    stack.enter_context(patch.object(rf, method))
                stack.enter_context(patch.object(rf.signal, "signal"))
                stack.enter_context(patch.object(rf.time, "sleep"))
                stack.enter_context(patch.object(rf, "health_check_ok", return_value=True))
                stack.enter_context(patch.object(rf, "start_proxy", return_value=Mock(process=Mock(poll=Mock(return_value=None)))))
                server = stack.enter_context(
                    patch.object(rf, "start_server", return_value=Mock(process=Mock(poll=Mock(return_value=None))))
                )
                prepare = stack.enter_context(patch.object(rf, "prepare_game_directory"))
                prepare_agent = stack.enter_context(patch.object(rf, "prepare_agent_run", return_value=example_preparation()))
                game = stack.enter_context(
                    patch.object(
                        controller, "start_agent",
                        return_value=rf.RunningContainer("agent-container", Mock(poll=Mock(return_value=0))),
                    )
                )
                self.assertEqual(controller.run_scheduler(config, names, mapping, images=IMAGES, backend=backend), 0)
                server.assert_called_once_with(
                    names.servers["counter"], names.internet_network, names.internal_network,
                    config.environment, "public-alias", "counter",
                    image=IMAGES.server, paths=config.paths,
                    server_inactivity_timeout_seconds=1800, server_terminal_grace_seconds=600,
                )
                prepare.assert_called_once_with("public-alias", paths=config.paths)
                prepare_agent.assert_called_once_with(
                    config.agent, rf.RunPaths().run_dir / "public-alias",
                    f"http://{names.servers['counter']}:{rf.SERVER_PORT}", "public-alias", config.agent_parameters,
                    initial_files_dir=None,
                )
                self.assertIs(game.call_args.args[-1], prepare_agent.return_value)
                self.assertEqual(game.call_args.kwargs["agent_image"], IMAGES.agent)
                self.assertIs(game.call_args.kwargs["backend"], backend)
                self.assertTrue(callable(game.call_args.kwargs["check_cancelled"]))

    def test_controllers_choose_account_slots_or_one_slot_per_game(self) -> None:
        mapping = {"alias-one": "first", "alias-two": "second", "alias-three": "third"}
        for backend_name in BACKENDS:
            with self.subTest(backend=backend_name):
                config = replace(make_config(backend_name), games=list(mapping.values()))
                if backend_name == rb.CODEX_CHATGPT:
                    config = replace(config, accounts=["codex01", "codex02"])
                backend = make_backend(config)
                names = rf.build_container_names(config.games)
                with patch.object(rf, "run_scheduler", return_value=0) as scheduler, patch.object(controller, "start_agent") as launch, patch.object(rf, "prepare_agent_run", return_value=example_preparation()) as prepare:
                    self.assertEqual(controller.run_scheduler(config, names, mapping, images=IMAGES, backend=backend), 0)
                    self.assertEqual(scheduler.call_args.args, (config.environment, names, mapping))
                    self.assertEqual(scheduler.call_args.kwargs["images"], IMAGES)
                    expected_slots = config.accounts if backend_name == rb.CODEX_CHATGPT else [
                        "api-worker-0", "api-worker-1", "api-worker-2",
                    ]
                    self.assertEqual(scheduler.call_args.kwargs["slots"], expected_slots)
                    callback = scheduler.call_args.kwargs["launch_agent"]
                    check_cancelled = Mock()
                    callback(expected_slots[0], "alias-one", "first", check_cancelled)
                    expected_args = (
                        expected_slots[0], "alias-one", "first", names.agents["first"], names.internal_network,
                        names.servers["first"], names.proxy, prepare.return_value,
                    )
                    expected_kwargs = {"agent_image": IMAGES.agent, "backend": backend,
                                       "check_cancelled": check_cancelled}
                    launch.assert_called_once_with(*expected_args, **expected_kwargs)

    def test_scheduler_applies_service_tier_before_each_agent_launch(self) -> None:
        for enabled in (True, False):
            with self.subTest(enabled=enabled), ExitStack() as stack:
                base = make_config(rb.CODEX_CHATGPT)
                config = replace(base, codex_account_fast_service_tier=enabled, parsed_run_config={
                    **base.parsed_run_config, "agent_parameters": {
                        **base.agent_parameters, "codex_account_fast_service_tier": enabled,
                    },
                })
                backend = make_backend(config)
                names = rf.build_container_names(config.games)
                scheduler = stack.enter_context(patch.object(rf, "run_scheduler", return_value=0))
                stack.enter_context(patch.object(rf, "prepare_agent_run", return_value=example_preparation()))
                stack.enter_context(patch.object(rf, "log_info"))
                clean = stack.enter_context(patch.object(rb, "clean_codex_account"))

                def launch(*_args, **_kwargs):
                    clean.assert_called_once_with("codex01", account_lock_fd=123,
                                                  fast_service_tier=enabled, paths=config.paths)
                    return Mock()

                popen = stack.enter_context(patch.object(controller.subprocess, "Popen", side_effect=launch))
                controller.run_scheduler(config, names, {"alias": "counter"}, images=IMAGES, backend=backend)
                callback = scheduler.call_args.kwargs["launch_agent"]
                for alias in ("first-attempt", "second-attempt"):
                    clean.reset_mock()
                    callback("codex01", alias, "counter", lambda: None)
                self.assertEqual(popen.call_count, 2)

    def test_account_cleanup_adds_fast_tier_without_duplicates_and_removes_it_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = rf.RunPaths(Path(directory) / "experiment with spaces")
            accounts = paths.accounts_dir
            account = accounts / "codex01"
            account.mkdir(parents=True)
            auth = account / "auth.json"
            auth.write_text('{"test": "not a real account"}\n', encoding="utf-8")
            auth_contents = auth.read_bytes()
            binary_dir = Path(directory) / "bin"
            binary_dir.mkdir()
            docker = binary_dir / "docker"
            docker.write_text('#!/bin/sh\n[ "$1" = ps ] && exit 0\nexit 99\n')
            docker.chmod(0o755)
            with patch.dict(os.environ, {"PATH": f"{binary_dir}:{os.environ['PATH']}"}), \
                    account_lock(account, kind="codex") as descriptor:
                for enabled in (False, True, True, False, True):
                    with self.subTest(enabled=enabled):
                        # Exercise the real shell cleanup and inherited lock,
                        # only on this synthetic account with a fake Docker CLI.
                        rb.clean_codex_account("codex01", account_lock_fd=descriptor,
                                               fast_service_tier=enabled, paths=paths)
                        saved = tomllib.loads((account / "config.toml").read_text(encoding="utf-8"))
                        expected = {"web_search": "disabled"}
                        if enabled:
                            expected["service_tier"] = "fast"
                        self.assertEqual(saved, expected)
                        self.assertEqual(auth.read_bytes(), auth_contents)

    def test_failed_account_cleanup_does_not_write_service_tier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = rf.RunPaths(Path(directory))
            account = paths.accounts_dir / "codex01"
            account.mkdir(parents=True)
            config = account / "config.toml"
            original = 'web_search = "disabled"\n'
            config.write_text(original, encoding="utf-8")
            with patch.object(rb, "run_reserved_command", side_effect=RuntimeError("clean failed")), \
                    self.assertRaisesRegex(RuntimeError, "clean failed"):
                rb.clean_codex_account("codex01", account_lock_fd=123, fast_service_tier=True, paths=paths)
            self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_agent_commands_do_not_receive_backend_variables_parameters_or_real_game_ids(self) -> None:
        names = rf.build_container_names(["ls20"])
        for backend_name in BACKENDS:
            with self.subTest(backend=backend_name), patch.dict(
                os.environ, {"ARC_API_KEY": "backend-key", "OPENAI_API_KEY": "agent-key"}
            ):
                backend = make_backend(make_config(backend_name))
                args = (
                    names.agents["ls20"], names.internal_network,
                    names.servers["ls20"], names.proxy, example_preparation("public-alias"),
                )
                command = controller.docker_agent_command(
                    "codex01" if backend_name == rb.CODEX_CHATGPT else "api-worker-0", *args,
                    agent_image=IMAGES.agent, backend=backend,
                )
                # Host mount-source paths may contain the repository's name. Check the
                # configuration and command actually supplied to the agent process.
                image_reference = IMAGES.agent
                agent_inputs = environment_arguments(command) + command[command.index(image_reference) + 1:]
                for private in ("ARC_API_KEY", "backend-key", "ls20", "arc3", "--environment", "--parameters"):
                    self.assertNotIn(private, repr(agent_inputs))
                self.assertNotIn("arc", " ".join(environment_arguments(command)).lower())
                if backend_name == rb.CODEX_API_KEY:
                    self.assertIn("CODEX_API_KEY", environment_arguments(command))
                    self.assertEqual(backend.process_environment()["CODEX_API_KEY"], "agent-key")
                    self.assertNotIn("agent-key", repr(command))
                for variable in ("ENV_SERVER_URL", "ENV_OBSERVATIONS_DIR", "ENV_CLIENT_DIR"):
                    self.assertFalse(any(value.startswith(variable + "=") for value in environment_arguments(command)))
                self.assertIn(f"HTTP_PROXY=http://{names.proxy}:{rf.PROXY_PORT}", environment_arguments(command))
                mounts = [command[index + 1] for index, part in enumerate(command) if part == "-v"]
                game_dir = rf.RunPaths().run_dir / "public-alias"
                self.assertIn(f"{game_dir / 'client'}:/client:rw", mounts)
                self.assertIn(f"{game_dir / 'observations'}:/observations:rw", mounts)
                self.assertIn(f"{game_dir / 'run'}:/home/user/run:rw", mounts)
                self.assertFalse(any("/server" in mount for mount in mounts))
                self.assertEqual([command[i + 1] for i, part in enumerate(command) if part == "--network"], [names.internal_network])

    def test_agent_memory_limits_process_data_and_container_memory_not_address_space(self) -> None:
        for backend_name in BACKENDS:
            with self.subTest(backend=backend_name), patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
                args = ("agent", "internal", "server", "proxy", example_preparation("alias", "gpt-5.6-sol", "max"))
                backend = make_backend(make_config(backend_name))
                command = controller.docker_agent_command(
                    "codex01" if backend_name == rb.CODEX_CHATGPT else "api-worker-0", *args,
                    agent_image=IMAGES.agent, backend=backend,
                )
                self.assertIn("--pull=never", command)
                image_index = command.index(IMAGES.agent)
                self.assertEqual(command[command.index("--memory") + 1], "10737418240")
                self.assertEqual(command[command.index("--memory-swap") + 1], "10737418240")
                limits = [command[index + 1] for index, part in enumerate(command) if part == "--ulimit"]
                self.assertEqual(limits, ["data=2147483648:2147483648"])
                self.assertLess(command.index("--memory"), image_index)
                self.assertLess(command.index("--memory-swap"), image_index)
                self.assertLess(command.index("--ulimit"), image_index)
                self.assertNotIn("ulimit -v", " ".join(command))
                self.assertEqual(command[image_index + 1:], [
                    "python3", "agent_runner.py", "alias", "--model", "gpt-5.6-sol", "--reasoning-effort", "max",
                ])

    def test_custom_memory_limits_reach_each_scheduled_docker_launch(self) -> None:
        runner = rf.parse_runner_config({"runner": {
            "container_memory_gib": 12.5,
            "process_data_soft_limit_gib": 1.5,
            "process_data_hard_limit_gib": 3.25,
        }}, Path("config.yaml"))
        for backend_name in BACKENDS:
            with self.subTest(backend=backend_name), ExitStack() as stack:
                config = replace(make_config(backend_name), runner=runner)
                backend = make_backend(config)
                names = rf.build_container_names(config.games)
                scheduler = stack.enter_context(patch.object(rf, "run_scheduler", return_value=0))
                stack.enter_context(patch.object(rf, "prepare_agent_run", return_value=example_preparation()))
                stack.enter_context(patch.object(rf, "log_info"))
                stack.enter_context(patch.object(rb, "clean_codex_account"))
                stack.enter_context(patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}))
                popen = stack.enter_context(patch.object(controller.subprocess, "Popen"))
                controller.run_scheduler(config, names, {"alias": "counter"}, images=IMAGES, backend=backend)
                callback = scheduler.call_args.kwargs["launch_agent"]
                for alias in ("first-game", "second-game"):
                    callback("codex01", alias, "counter", lambda: None)
                self.assertEqual(popen.call_count, 2)
                for call in popen.call_args_list:
                    command = call.args[0]
                    self.assertEqual(command[command.index("--memory") + 1], "13421772800")
                    self.assertEqual(command[command.index("--memory-swap") + 1], "13421772800")
                    self.assertEqual(command[command.index("--ulimit") + 1], "data=1610612736:3489660928")

    def test_container_names_and_client_hostnames_are_neutral(self) -> None:
        names = rf.build_container_names(["ls20", "ARC-private-game"])
        all_names = [
            names.internal_network, names.internet_network, names.proxy, *names.servers.values(), *names.agents.values()
        ]
        self.assertEqual(len(set(all_names)), len(all_names))
        self.assertNotIn("arc", " ".join(all_names).lower())
        self.assertNotIn("ls20", " ".join(all_names))

    def test_both_controllers_copy_only_selected_helpers_and_keep_runtime_description_authoritative(self) -> None:
        for backend_name in BACKENDS:
            for environment in ("arc3", "agimaze_single", "example"):
                with self.subTest(backend=backend_name, environment=environment):
                    with tempfile.TemporaryDirectory() as directory:
                        game_dir = prepare_game("public-alias", environment, paths=rf.RunPaths(Path(directory)))
                        workspace = game_dir / "run" / "workspace_init"
                        self.assertEqual(workspace.is_dir(), environment == "arc3")
                        self.assertFalse((workspace / "main_prompt.md").exists())
                        self.assertTrue((game_dir / "run" / "prompts" / "main_prompt.md").is_file())
                        self.assertEqual((workspace / "ascii_to_png.py").is_file(), environment == "arc3")
                        self.assertFalse((workspace / "environment_description.md").exists())
                        for private_file in ("environment_manager.py", "run_config.yaml", "requirements.txt"):
                            self.assertFalse(list(workspace.rglob(private_file)))
                        self.assertFalse((workspace / "client").exists())
                        self.assertFalse((game_dir / "run" / "client").exists())
                        self.assertEqual(list((game_dir / "observations").iterdir()), [])
                        self.assertEqual(list((game_dir / "server").iterdir()), [])
                        self.assertFalse((game_dir / "client" / ".client_session.json").exists())
                        self.assertTrue((game_dir / "client" / ".client.lock").is_file())
                        self.assertEqual(
                            json.loads((game_dir / "client" / "client_config.json").read_text(encoding="utf-8")),
                            {"server_url": f"http://env-server:{rf.SERVER_PORT}", "observations_path": "/observations",
                             "http_read_timeout_seconds": 3900.0},
                        )
                        for directory in (game_dir / "client", game_dir / "observations"):
                            for path in (directory, *directory.rglob("*")):
                                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o555 if path.is_dir() else 0o444)
                                self.assertEqual(path.stat().st_uid, os.getuid())
                        client = (game_dir / "client" / "client.py").read_text(encoding="utf-8")
                        self.assertNotIn("ENV_SERVER_URL", client)
                        self.assertNotIn("ENV_OBSERVATIONS_DIR", client)
                        self.assertNotIn("ARC_API_KEY", client)


class ShutdownPermissionsTest(unittest.TestCase):
    @contextmanager
    def controller_run(self, backend_name):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            run_dir = Path(directory) / "run"
            stack.enter_context(patch.object(controller, "parse_run_config", return_value=make_config(backend_name, paths=rf.RunPaths(Path(directory)))))
            stack.enter_context(patch.object(rb.RunBackend, "reserve", return_value=nullcontext()))
            stack.enter_context(patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True))
            for method in ("verify_container_names_available", "verify_network_names_available", "setup_runner_logging", "log_info", "LOGGER"):
                stack.enter_context(patch.object(rf, method))
            stack.enter_context(patch.object(rf, "build_dockers"))
            stack.enter_context(patch.object(rf, "image_exists", return_value=False))
            stack.enter_context(patch.object(rf, "remove_run_images"))
            stack.enter_context(patch("sys.stderr", new_callable=io.StringIO))
            if backend_name == rb.CODEX_CHATGPT:
                stack.enter_context(patch.object(rb, "verify_codex_accounts"))
            else:
                stack.enter_context(patch.object(rb, "verify_codex_api_key"))
                stack.enter_context(patch.object(rb, "confirm_api_key_run"))
            # Do not let a missed mock start Docker or change the real run directory.
            stack.enter_context(patch.object(rf.subprocess, "run", side_effect=AssertionError("Unexpected subprocess")))
            stack.enter_context(patch.object(rf.subprocess, "Popen", side_effect=AssertionError("Unexpected subprocess")))
            yield run_dir, stack

    def create_readonly_output(self, run_dir):
        observations = run_dir / "alias" / "observations"
        observations.mkdir(parents=True)
        output = observations / "final.txt"
        output.write_text("observation", encoding="utf-8")
        output.chmod(0o444)
        observations.chmod(0o555)
        run_dir.chmod(0o555)
        return output

    def assert_writable(self, run_dir):
        for path in (run_dir, *run_dir.rglob("*")):
            self.assertTrue(path.stat().st_mode & stat.S_IWUSR, str(path))
        self.assertEqual((run_dir / "alias" / "observations" / "final.txt").read_text(), "observation")

    def test_both_controllers_restore_permissions_after_actual_scheduler_cleanup(self) -> None:
        for backend_name in BACKENDS:
            with self.subTest(backend=backend_name), self.controller_run(backend_name) as (run_dir, stack):
                stopped = []

                def launch(*args, **kwargs):
                    self.create_readonly_output(run_dir)
                    return rf.RunningContainer("agent-container", Mock(poll=Mock(return_value=0)))

                def stop(container_name, process, name):
                    self.assertEqual(stat.S_IMODE((run_dir / "alias" / "observations").stat().st_mode), 0o555)
                    stopped.append(name)

                for method in ("create_network", "remove_network"):
                    stack.enter_context(patch.object(rf, method))
                stack.enter_context(patch.object(rf.signal, "signal"))
                stack.enter_context(patch.object(rf, "health_check_ok", return_value=True))
                for name in ("proxy", "server"):
                    stack.enter_context(patch.object(
                        rf, f"start_{name}", return_value=rf.RunningContainer(name, Mock(poll=Mock(return_value=None))),
                    ))
                stack.enter_context(patch.object(rf, "stop_container", side_effect=stop))
                stack.enter_context(patch.object(controller, "start_agent", side_effect=launch))
                stack.enter_context(patch.object(rf.time, "sleep"))
                self.assertEqual(controller.main(["--run-root", str(run_dir.parent)]), 0)
                self.assertEqual(stopped, ["agent for game counter", "server for game counter", "proxy"])
                self.assert_writable(run_dir)

    def test_both_controllers_restore_permissions_on_failure_and_handled_interruption(self) -> None:
        for backend_name in BACKENDS:
            for outcome in (17, 130, RuntimeError("Launch failed")):
                with self.subTest(backend=backend_name, outcome=outcome), self.controller_run(backend_name) as (run_dir, stack):
                    def schedule(*args, **kwargs):
                        self.create_readonly_output(run_dir)
                        if isinstance(outcome, Exception):
                            raise outcome
                        return outcome

                    stack.enter_context(patch.object(controller, "run_scheduler", side_effect=schedule))
                    self.assertEqual(controller.main(["--run-root", str(run_dir.parent)]), 1 if isinstance(outcome, Exception) else outcome)
                    self.assert_writable(run_dir)

    def test_keyboard_interrupt_also_restores_permissions(self) -> None:
        for backend_name in BACKENDS:
            with self.subTest(backend=backend_name), self.controller_run(backend_name) as (run_dir, stack):
                def schedule(*args, **kwargs):
                    self.create_readonly_output(run_dir)
                    raise KeyboardInterrupt

                stack.enter_context(patch.object(controller, "run_scheduler", side_effect=schedule))
                self.assertEqual(controller.main(["--run-root", str(run_dir.parent)]), 130)
                self.assert_writable(run_dir)

    def test_startup_failure_after_creating_run_also_restores_permissions(self) -> None:
        for backend_name in BACKENDS:
            with self.subTest(backend=backend_name), self.controller_run(backend_name) as (run_dir, stack):
                def build(*args, **kwargs):
                    self.create_readonly_output(run_dir)
                    raise RuntimeError("Build failed")

                stack.enter_context(patch.object(rf, "build_dockers", side_effect=build))
                scheduler = stack.enter_context(patch.object(controller, "run_scheduler"))
                self.assertEqual(controller.main(["--run-root", str(run_dir.parent)]), 1)
                scheduler.assert_not_called()
                self.assert_writable(run_dir)

    def test_refused_start_does_not_change_an_existing_run(self) -> None:
        for backend_name in BACKENDS:
            with self.subTest(backend=backend_name), self.controller_run(backend_name) as (run_dir, stack):
                output = self.create_readonly_output(run_dir)
                restore = stack.enter_context(patch.object(rf, "make_run_writable", wraps=rf.make_run_writable))
                self.assertEqual(controller.main(["--run-root", str(run_dir.parent)]), 1)
                restore.assert_not_called()
                self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o555)
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o444)


class DockerBuildTest(unittest.TestCase):
    def test_build_wrapper_passes_selected_environment(self) -> None:
        with (
            patch.object(rf.subprocess, "Popen", return_value=Mock(wait=Mock(return_value=0))) as popen,
            patch.object(rf.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="sha256:" + "1" * 64)) as inspect,
            patch.object(rf, "log_info"),
        ):
            self.assertIsNone(rf.build_dockers("example", "agent_v1", IMAGES))
        popen.assert_called_once_with(
            ["bash", str(rf.BUILD_DOCKERS_SCRIPT), "example", "agent_v1", IMAGES.agent, IMAGES.server, IMAGES.proxy],
            cwd=ROOT, start_new_session=True,
        )
        self.assertEqual(inspect.call_count, 3)

    def test_build_wrapper_passes_agent_docker_build_args_as_literal_arguments(self) -> None:
        value = "0.153.4 $HOME $(printf unexpected) `printf unexpected` spaces=preserved"
        with (
            patch.object(rf.subprocess, "Popen", return_value=Mock(wait=Mock(return_value=0))) as popen,
            patch.object(rf.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="sha256:" + "1" * 64)),
            patch.object(rf, "log_info"),
        ):
            rf.build_dockers("example", "agent_v1_08_wstop3", IMAGES,
                             agent_docker_build_args={"CODEX_VERSION": value})
        self.assertEqual(popen.call_args.args[0], [
            "bash", str(rf.BUILD_DOCKERS_SCRIPT), "example", "agent_v1_08_wstop3",
            IMAGES.agent, IMAGES.server, IMAGES.proxy, "--agent-build-arg", f"CODEX_VERSION={value}",
        ])

    def run_build_script(self, *args, existing_image="", script_path=None):
        # Stub Docker inside Bash: exercise the actual script without building images.
        script = r'''
docker() {
    if [[ "$1" == image ]]; then
        if [[ "${@: -1}" == "reference=$EXISTING_IMAGE" ]]; then
            printf 'existing-image\n'
        fi
        return 0
    fi
    printf "%s\0" "$@"
    printf "\0"
}
export -f docker
bash "$@"
'''
        return subprocess.run(
            ["bash", "-c", script, "build-test", str(script_path or rf.BUILD_DOCKERS_SCRIPT), *args],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
            env=dict(os.environ, EXISTING_IMAGE=existing_image),
        )

    def test_build_script_passes_overrides_only_to_agent_with_either_tag_mode(self) -> None:
        value = "0.153.4 $HOME $(printf unexpected) `printf unexpected` spaces=preserved\nnext line"
        for agent in ("agent_v1", "agent_v1_01_wstop", "agent_v1_08_wstop3"):
            for tags in ([], [IMAGES.agent, IMAGES.server, IMAGES.proxy]):
                with self.subTest(agent=agent, tags=tags):
                    result = self.run_build_script("example", agent, *tags,
                                                  "--agent-build-arg", f"CODEX_VERSION={value}")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    commands = [command.split("\0") for command in result.stdout.split("\0\0") if command]
                    self.assertEqual(len(commands), 3)
                    self.assertIn(f"CODEX_VERSION={value}", commands[0])
                    self.assertNotIn(f"CODEX_VERSION={value}", commands[1] + commands[2])
                    self.assertIn(f"USER_ID={os.getuid()}", commands[0])
                    self.assertIn(f"GROUP_ID={os.getgid()}", commands[0])

    def test_build_script_accepts_multiple_generic_arguments_including_empty_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "src"
            agent = source / "agents/custom_agent"
            agent.mkdir(parents=True)
            (agent / "prepare_run.py").touch()
            (agent / "Dockerfile").write_text("FROM scratch\nARG TOOL_VERSION\n  arg BUILD_LABEL\nARG EMPTY\n")
            environment = source / "server/environments/example"
            environment.mkdir(parents=True)
            (environment / "environment_manager.py").touch()
            script_path = source / "build_dockers.sh"
            script_path.write_text(rf.BUILD_DOCKERS_SCRIPT.read_text())
            result = self.run_build_script(
                "example", "custom_agent", "--agent-build-arg", "TOOL_VERSION=2",
                "--agent-build-arg", "BUILD_LABEL=hello world", "--agent-build-arg", "EMPTY=",
                script_path=script_path,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = [command.split("\0") for command in result.stdout.split("\0\0") if command]
        self.assertEqual(len(commands), 3)
        for argument in ("TOOL_VERSION=2", "BUILD_LABEL=hello world", "EMPTY="):
            self.assertIn(argument, commands[0])
            self.assertNotIn(argument, commands[1] + commands[2])

    def test_build_script_rejects_invalid_reserved_duplicate_and_undeclared_arguments(self) -> None:
        cases = [
            ("--agent-build-arg",), ("--agent-build-arg", "CODEX_VERSION"),
            ("--wrong-flag", "CODEX_VERSION=0.153.4"),
            ("--agent-build-arg", "CODEX_VERSION=0.153.4", "--agent-build-arg", "CODEX_VERSION=0.154.0"),
        ]
        cases.extend(("--agent-build-arg", f"{name}=value") for name in
                     ("", "BAD-NAME", "CODEX_VERSION\n", "USER_ID", "GROUP_ID", "CODEX_VERISON", "codex_version"))
        for args in cases:
            with self.subTest(args=args):
                result = self.run_build_script("example", "agent_v1_08_wstop3", *args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
        result = self.run_build_script("example", "test_arc3_agent_v1_02_wstop_wreset",
                                       "--agent-build-arg", "CODEX_VERSION=0.153.4")
        self.assertEqual(result.returncode, 2)
        self.assertIn("does not declare ARG CODEX_VERSION", result.stderr)

    def test_build_script_selects_environment_dependencies_only_for_server(self) -> None:
        for environment in ("arc3", "agimaze_single", "example"):
            result = self.run_build_script(environment, "agent_v1")
            self.assertEqual(result.returncode, 0, result.stderr)
            commands = [command.split("\0") for command in result.stdout.split("\0\0") if command]
            self.assertEqual(len(commands), 3)
            self.assertIn(f"ENVIRONMENT={environment}", commands[1])
            self.assertEqual(commands[0][commands[0].index("-f") + 1], str(ROOT / "src/agents/agent_v1/Dockerfile"))
            self.assertEqual(commands[0][-1], str(ROOT / "src"))
            self.assertNotIn(f"ENVIRONMENT={environment}", commands[0] + commands[2])
            self.assertNotIn("--iidfile", commands[0] + commands[1] + commands[2])
            self.assertEqual(
                [command[command.index("-t") + 1].split(":")[0] for command in commands],
                ["unified-agent", "unified-server", "unified-proxy"],
            )
            suffixes = [command[command.index("-t") + 1].split(":")[1] for command in commands]
            self.assertEqual(len(set(suffixes)), 1)
            self.assertRegex(suffixes[0], r"^run-[0-9a-f]{32}$")
            self.assertNotIn("ARC_API_KEY", result.stdout)

    def test_build_script_uses_all_three_controller_supplied_tags(self) -> None:
        tags = [IMAGES.agent, IMAGES.server, IMAGES.proxy]
        result = self.run_build_script("example", "agent_v1", *tags)
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = [command.split("\0") for command in result.stdout.split("\0\0") if command]
        self.assertEqual([command[command.index("-t") + 1] for command in commands], tags)

    def test_build_script_refuses_to_overwrite_an_existing_run_before_any_build(self) -> None:
        result = self.run_build_script(
            "example", "agent_v1", IMAGES.agent, IMAGES.server, IMAGES.proxy, existing_image=IMAGES.server,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("Refusing to replace", result.stderr)

    def test_account_scripts_use_only_the_separate_account_image(self) -> None:
        for script in ("check_account.sh", "check_account_run.sh", "check_codex_with_api_key.sh", "prepare_account.sh"):
            content = (ROOT / "codex_accounts" / script).read_text(encoding="utf-8")
            self.assertIn('"$CODEX_ACCOUNTS_IMAGE"', content)
            self.assertIn("CODEX_ACCOUNTS_IMAGE=$(", content)
            self.assertIn('build_docker.sh" --if-missing', content)
            if script == "check_account.sh":
                self.assertIn('check_account.py"', content)
                content += (ROOT / "codex_accounts" / "check_account.py").read_text(encoding="utf-8")
            self.assertIn("--pull=never", content)
            self.assertNotIn("codex-agent-unified-agent", content)

    def test_build_script_rejects_missing_unknown_or_unsafe_environment_before_docker(self) -> None:
        for args in (
            (), ("unknown",), ("../arc3",), ("arc3", ""), ("arc3", "image-id.txt", "extra"),
            ("arc3", "agent_v1", "agent:latest", IMAGES.server, IMAGES.proxy),
            ("arc3", "agent_v1", IMAGES.agent, IMAGES.agent, IMAGES.proxy),
        ):
            result = self.run_build_script(*args)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")

    def test_server_dependency_install_handles_optional_requirements(self) -> None:
        dockerfile = (ROOT / "src" / "Dockerfile.server").read_text(encoding="utf-8")
        self.assertIn("COPY server/requirements.txt", dockerfile)
        self.assertIn("-r /tmp/server-requirements.txt", dockerfile)
        self.assertNotIn("arc-agi==", dockerfile)
        install = re.search(r"RUN test -n.*?(?=\n\n)", dockerfile, re.DOTALL)
        self.assertIsNotNone(install)
        shell_command = install.group()[len("RUN "):].replace("\\\n", "")
        for environment in ("arc3", "agimaze_single", "example"):
            result = subprocess.run(
                ["bash", "-c", 'python() { printf "%s\\n" "$@"; }\n' + shell_command],
                cwd=ROOT / "src", env=dict(os.environ, ENVIRONMENT=environment),
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            if environment == "arc3":
                self.assertIn("server/environments/arc3/requirements.txt", result.stdout)
            else:
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
