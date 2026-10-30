from __future__ import annotations

import datetime
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_controller as accounts_controller
import run_controller_with_api_key as api_controller
import run_funs as rf

CONTROLLERS = (accounts_controller, api_controller)


class RunConfigurationTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config_path = self.directory / "config.yaml"
        self.paths = rf.RunPaths(self.directory)
        accounts = self.paths.accounts_dir
        (accounts / "codex01").mkdir(parents=True)

    def write_config(self, **settings):
        config = {"agent": "agent_v1", "environment": "example", "games": ["counter"], "codex_accounts": ["codex01"]}
        config.update(settings)
        self.config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        return config

    def test_default_run_paths_use_the_source_directory(self) -> None:
        for controller in CONTROLLERS:
            args = controller.parse_args([])
            paths = rf.RunPaths(args.run_root)
            self.assertEqual(paths.root, ROOT)
            self.assertEqual(paths.resolve_config(args.run_config), ROOT / "run_config.yaml")
            self.assertEqual(paths.accounts_dir, ROOT / "codex_accounts")
            self.assertEqual(paths.run_dir, ROOT / "run")
            self.assertEqual(paths.run_tb_dir, ROOT / "run_tb")
            self.assertEqual(paths.runner_log, ROOT / "run/runner.log")

    def test_relative_root_and_explicit_config_use_caller_cwd_independently(self) -> None:
        with patch("os.getcwd", return_value=str(self.directory)):
            for controller in CONTROLLERS:
                args = controller.parse_args(["--run-root", "experiment with spaces", "--run-config", "chosen.yaml"])
                paths = rf.RunPaths(args.run_root)
                self.assertEqual(paths.root, self.directory / "experiment with spaces")
                self.assertEqual(paths.resolve_config(None), paths.root / "run_config.yaml")
                self.assertEqual(paths.resolve_config(args.run_config), self.directory / "chosen.yaml")
                self.assertEqual(paths.resolve_config(self.config_path), self.config_path)

    def test_external_root_uses_its_accounts_but_source_agent_build_settings(self) -> None:
        (self.paths.accounts_dir / "external_only").mkdir()
        fake_agent = self.directory / "src/agents/agent_v1_07_wstop2"
        fake_agent.mkdir(parents=True)
        (fake_agent / "Dockerfile").write_text("FROM scratch\nARG WRONG_VERSION\n")
        self.write_config(agent="agent_v1_07_wstop2", codex_accounts=["external_only"],
                          agent_docker_build_args={"CODEX_VERSION": "0.166.0"})
        for controller in CONTROLLERS:
            config = controller.parse_run_config(self.config_path, paths=self.paths)
            self.assertEqual(config.paths, self.paths)
            self.assertEqual(config.agent_docker_build_args, {"CODEX_VERSION": "0.166.0"})
        self.assertEqual(rf.agent_directory("agent_v1_07_wstop2"), ROOT / "src/agents/agent_v1_07_wstop2")
        self.assertEqual(accounts_controller.CHECK_CODEX_ACCOUNT_SCRIPT, ROOT / "codex_accounts/check_account.sh")
        self.assertEqual(accounts_controller.CLEAN_CODEX_ACCOUNT_SCRIPT, ROOT / "codex_accounts/clean_account.sh")
        self.assertEqual(api_controller.CHECK_CODEX_API_KEY_SCRIPT, ROOT / "codex_accounts/check_codex_with_api_key.sh")

    def test_both_controllers_parse_each_environment_without_loading_its_dependencies(self) -> None:
        with patch.dict(sys.modules, {"arc_agi": None, "arcengine": None}):
            for name, game, parameters in (
                ("arc3", "ls20", {"competition": False, "tag": "api-test"}),
                ("agimaze_single", "TRAINING/S0-keys/STAGE-01/0000.json", {"seed": 42}),
                ("example", "counter", {"target": 3}),
            ):
                self.write_config(environment=name, games=[game], environment_parameters=parameters)
                for controller in CONTROLLERS:
                    with self.subTest(controller=controller.__name__, environment=name):
                        config = controller.parse_run_config(self.config_path, paths=self.paths)
                        self.assertEqual(config.environment, rf.EnvironmentConfig(name, parameters, []))
                        self.assertEqual(config.games, [game])

    def test_supplied_config_runs_one_noncompetition_arc3_game(self) -> None:
        for controller in CONTROLLERS:
            config = controller.parse_run_config(ROOT / "run_config_arc3.yaml", paths=self.paths)
            self.assertEqual(config.games, ["ls20"])
            self.assertEqual(
                config.environment,
                rf.EnvironmentConfig("arc3", {"competition": False, "tag": "api-test"}, ["ARC_API_KEY"]),
            )
            self.assertEqual(config.parsed_run_config["model"], "gpt-5.6-sol")
            self.assertEqual(config.parsed_run_config["reasoning_effort"], "max")

    def test_parameters_and_variable_allowlist_default_to_empty(self) -> None:
        self.write_config()
        with patch.dict(os.environ, {}, clear=True):
            for controller in CONTROLLERS:
                config = controller.parse_run_config(self.config_path, paths=self.paths)
                self.assertEqual(config.environment, rf.EnvironmentConfig("example", {}, []))
                rf.verify_environment_variables(config.environment)

    def test_runner_defaults_preserve_existing_byte_limits(self) -> None:
        expected = rf.RunnerConfig(10737418240, 2147483648, 2147483648)
        for settings in ({}, {"runner": {}}):
            self.write_config(**settings)
            for controller in CONTROLLERS:
                with self.subTest(settings=settings, controller=controller.__name__):
                    self.assertEqual(controller.parse_run_config(self.config_path, paths=self.paths).runner, expected)

    def test_agent_docker_build_args_default_to_empty_for_all_agents(self) -> None:
        for agent in ("agent_v1", "agent_v1_01_wstop", "agent_v1_07_wstop2", "test_arc3_agent_v1_02_wstop_wreset"):
            for settings in ({}, {"agent_docker_build_args": {}}):
                self.write_config(agent=agent, **settings)
                for controller in CONTROLLERS:
                    with self.subTest(agent=agent, settings=settings, controller=controller.__name__):
                        self.assertEqual(controller.parse_run_config(self.config_path, paths=self.paths).agent_docker_build_args, {})

    def test_both_controllers_accept_codex_version_for_opted_in_agent(self) -> None:
        arguments = {"CODEX_VERSION": "0.153.4"}
        self.write_config(agent="agent_v1_07_wstop2", agent_docker_build_args=arguments)
        for controller in CONTROLLERS:
            with self.subTest(controller=controller.__name__):
                config = controller.parse_run_config(self.config_path, paths=self.paths)
                self.assertEqual(config.agent_docker_build_args, arguments)
                self.assertEqual(config.parsed_run_config["agent_docker_build_args"], arguments)

    def test_agent_docker_build_args_are_generic_and_preserve_string_values(self) -> None:
        agent = self.directory / "agents/custom_agent"
        agent.mkdir(parents=True)
        (agent / "prepare_run.py").touch()
        (agent / "Dockerfile").write_text(
            "FROM scratch\nARG TOOL_VERSION=1\n  arg BUILD_LABEL\nARG EMPTY=default\n",
            encoding="utf-8",
        )
        arguments = {"TOOL_VERSION": "2.0-beta.1", "BUILD_LABEL": "spaces = $HOME\nsecond line", "EMPTY": ""}
        self.write_config(agent="custom_agent", agent_docker_build_args=arguments)
        with patch.object(rf, "AGENTS_DIR", agent.parent):
            for controller in CONTROLLERS:
                self.assertEqual(controller.parse_run_config(self.config_path, paths=self.paths).agent_docker_build_args, arguments)

    def test_agent_docker_build_args_reject_invalid_mappings_names_and_values(self) -> None:
        invalid = [None, False, [], "CODEX_VERSION=0.153.4", 2]
        invalid.extend({name: "value"} for name in (None, 1, "", "BAD-NAME", "CODEX_VERSION=bad", "CODEX_VERSION\n"))
        invalid.extend({"CODEX_VERSION": value} for value in (None, True, 1, 1.5, [], {}, "bad\0value"))
        for arguments in invalid:
            self.write_config(agent="agent_v1_07_wstop2", agent_docker_build_args=arguments)
            for controller in CONTROLLERS:
                with self.subTest(arguments=arguments, controller=controller.__name__):
                    with self.assertRaisesRegex(RuntimeError, "agent_docker_build_args"):
                        controller.parse_run_config(self.config_path, paths=self.paths)

    def test_agent_docker_build_args_reject_reserved_and_undeclared_names(self) -> None:
        for name in ("USER_ID", "GROUP_ID", "CODEX_VERISON", "codex_version"):
            self.write_config(agent="agent_v1_07_wstop2", agent_docker_build_args={name: "123"})
            for controller in CONTROLLERS:
                with self.subTest(name=name, controller=controller.__name__):
                    expected = "managed by the controller" if name in {"USER_ID", "GROUP_ID"} else "does not declare ARG"
                    with self.assertRaisesRegex(RuntimeError, expected):
                        controller.parse_run_config(self.config_path, paths=self.paths)

    def test_other_agents_do_not_silently_accept_codex_version_override(self) -> None:
        for agent in ("agent_v1", "agent_v1_01_wstop", "test_arc3_agent_v1_02_wstop_wreset"):
            self.write_config(agent=agent, agent_docker_build_args={"CODEX_VERSION": "0.153.4"})
            for controller in CONTROLLERS:
                with self.subTest(agent=agent, controller=controller.__name__):
                    with self.assertRaisesRegex(RuntimeError, "does not declare ARG CODEX_VERSION"):
                        controller.parse_run_config(self.config_path, paths=self.paths)

    def test_supplied_runner_example_uses_defaults(self) -> None:
        for controller in CONTROLLERS:
            config = controller.parse_run_config(ROOT / "run_config.yaml", paths=self.paths)
            self.assertNotIn("runner", config.parsed_run_config)
            self.assertNotIn("recovery_delay_seconds", config.parsed_run_config)
            self.assertNotIn("max_recovery_attempts", config.parsed_run_config)
            self.assertEqual(config.runner, rf.RunnerConfig())

    def test_runner_accepts_fractions_and_defaults_each_missing_setting(self) -> None:
        cases = (
            ({"container_memory_gib": 12.5, "process_data_soft_limit_gib": 1.5,
              "process_data_hard_limit_gib": 2.75},
             rf.RunnerConfig(13421772800, 1610612736, 2952790016)),
            ({"container_memory_gib": 8}, rf.RunnerConfig(8589934592, 2147483648, 2147483648)),
            ({"process_data_soft_limit_gib": 0.1}, rf.RunnerConfig(10737418240, 107374182, 2147483648)),
            ({"process_data_hard_limit_gib": 3}, rf.RunnerConfig(10737418240, 2147483648, 3221225472)),
        )
        for runner, expected in cases:
            self.write_config(runner=runner)
            for controller in CONTROLLERS:
                with self.subTest(runner=runner, controller=controller.__name__):
                    config = controller.parse_run_config(self.config_path, paths=self.paths)
                    self.assertEqual(config.runner, expected)
                    self.assertEqual(config.parsed_run_config["runner"], runner)

    def test_runner_rejects_invalid_sections_and_unknown_settings(self) -> None:
        for runner in (None, False, [], "defaults", 2, {"swap_gib": 1}, {"container_memory_gb": 10}):
            self.write_config(runner=runner)
            for controller in CONTROLLERS:
                with self.subTest(runner=runner, controller=controller.__name__):
                    with self.assertRaisesRegex(RuntimeError, "runner"):
                        controller.parse_run_config(self.config_path, paths=self.paths)

    def test_runner_rejects_invalid_memory_values(self) -> None:
        for key in ("container_memory_gib", "process_data_soft_limit_gib", "process_data_hard_limit_gib"):
            for value in (None, True, False, "2", [], {}, 0, -1, float("nan"), float("inf"),
                          float("-inf"), 1e-20, 1e100):
                self.write_config(runner={key: value})
                for controller in CONTROLLERS:
                    with self.subTest(key=key, value=value, controller=controller.__name__):
                        with self.assertRaisesRegex(RuntimeError, f"runner.{key}"):
                            controller.parse_run_config(self.config_path, paths=self.paths)

    def test_runner_rejects_soft_limit_above_hard_limit_including_defaults(self) -> None:
        for runner in ({"process_data_soft_limit_gib": 3}, {"process_data_hard_limit_gib": 1},
                       {"process_data_soft_limit_gib": 2.5, "process_data_hard_limit_gib": 2.25}):
            self.write_config(runner=runner)
            for controller in CONTROLLERS:
                with self.subTest(runner=runner, controller=controller.__name__):
                    with self.assertRaisesRegex(RuntimeError, "soft_limit_gib.*less than or equal to.*hard_limit_gib"):
                        controller.parse_run_config(self.config_path, paths=self.paths)

    def test_account_fast_service_tier_defaults_to_false_and_accepts_booleans(self) -> None:
        self.write_config()
        self.assertIs(accounts_controller.parse_run_config(self.config_path, paths=self.paths).codex_account_fast_service_tier, False)
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.write_config(codex_account_fast_service_tier=enabled)
                config = accounts_controller.parse_run_config(self.config_path, paths=self.paths)
                self.assertIs(config.codex_account_fast_service_tier, enabled)

    def test_account_fast_service_tier_rejects_non_booleans(self) -> None:
        for value in (None, 0, 1, "true", "false", [], {}):
            with self.subTest(value=value):
                self.write_config(codex_account_fast_service_tier=value)
                with self.assertRaisesRegex(RuntimeError, "codex_account_fast_service_tier.*must be a boolean"):
                    accounts_controller.parse_run_config(self.config_path, paths=self.paths)

    def test_nested_parameters_are_preserved_without_environment_specific_interpretation(self) -> None:
        parameters = {
            "competition": True,
            "tag": "a tag with spaces",
            "custom": {"values": [None, True, 3, 1.5, "こんにちは"], "empty": {}},
        }
        parsed = rf.parse_environment_config(
            {"environment": "example", "environment_parameters": parameters}, self.config_path
        )
        self.assertEqual(parsed.parameters, parameters)

    def test_invalid_environment_names_and_missing_packages_are_rejected(self) -> None:
        for name in (None, "", 42, [], "../arc3", "/tmp/example", "arc3.environment_manager", "missing", "9bad"):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                rf.parse_environment_config({"environment": name}, self.config_path)

    def test_environment_symlink_cannot_select_a_package_outside_the_environments_directory(self) -> None:
        packages = self.directory / "environments"
        packages.mkdir()
        outside = self.directory / "outside"
        outside.mkdir()
        (outside / "environment_manager.py").write_text("", encoding="utf-8")
        (packages / "escape").symlink_to(outside, target_is_directory=True)
        with patch.object(rf, "ENVIRONMENTS_DIR", packages), self.assertRaises(RuntimeError):
            rf.environment_directory("escape")

    def test_non_json_parameters_and_coerced_mapping_keys_are_rejected(self) -> None:
        recursive = {}
        recursive["again"] = recursive
        invalid_parameters = (
            None, [], "target=3", False, {"x": b"bytes"}, {"x": {1, 2}},
            {"x": datetime.date(2026, 1, 1)}, {"x": float("nan")}, {"x": float("inf")},
            {1: "not a string key"}, {"nested": [{False: "not a string key"}]}, recursive,
        )
        for parameters in invalid_parameters:
            with self.subTest(parameters=parameters), self.assertRaisesRegex(RuntimeError, "environment_parameters"):
                rf.parse_environment_config(
                    {"environment": "example", "environment_parameters": parameters}, self.config_path
                )

    def test_legacy_settings_and_misspelling_have_migration_errors(self) -> None:
        for key in ("competition", "tag", "environmnet_parameters"):
            self.write_config(**{key: False})
            for controller in CONTROLLERS:
                with self.subTest(key=key, controller=controller.__name__):
                    with self.assertRaisesRegex(RuntimeError, "environment_parameters"):
                        controller.parse_run_config(self.config_path, paths=self.paths)

    def test_duplicate_games_remain_rejected(self) -> None:
        self.write_config(games=["counter", "counter"])
        for controller in CONTROLLERS:
            with self.assertRaisesRegex(RuntimeError, "games must be unique"):
                controller.parse_run_config(self.config_path, paths=self.paths)

    def test_variables_are_names_only_and_invalid_entries_are_not_echoed(self) -> None:
        secret = "secret-value-must-not-be-printed"
        for variables in (
            None, "API_KEY", {"API_KEY": secret}, [42], [""], ["9KEY"], ["API-KEY"],
            [" API_KEY"], ["API_KEY\n"], [f"API_KEY={secret}"],
        ):
            with self.subTest(variables=variables), self.assertRaises(RuntimeError) as error:
                rf.parse_environment_config(
                    {"environment": "example", "environment_variables": variables}, self.config_path
                )
            self.assertNotIn(secret, str(error.exception))

    def test_duplicate_and_launcher_managed_variable_names_are_rejected(self) -> None:
        for variables in (["API_KEY", "API_KEY"], *([name] for name in rf.SERVER_MANAGED_VARIABLES)):
            with self.subTest(variables=variables), self.assertRaises(RuntimeError):
                rf.parse_environment_config(
                    {"environment": "example", "environment_variables": variables}, self.config_path
                )

    def test_required_variables_are_checked_without_storing_their_values(self) -> None:
        config = rf.parse_environment_config(
            {"environment": "example", "environment_variables": ["API_KEY", "EMPTY_SETTING"]},
            self.config_path,
        )
        with patch.dict(os.environ, {"API_KEY": "private-value"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "EMPTY_SETTING") as error:
                rf.verify_environment_variables(config)
            self.assertNotIn("private-value", str(error.exception))
            os.environ["EMPTY_SETTING"] = ""
            rf.verify_environment_variables(config)
            self.assertEqual(config.variables, ["API_KEY", "EMPTY_SETTING"])
            self.assertNotIn("private-value", repr(config))

    def test_both_controllers_resolve_server_mounts_from_run_root(self) -> None:
        socket_dir = self.directory / "run_tb" / "socket with spaces"
        socket_dir.mkdir(parents=True)
        self.write_config(environment="terminal_bench", games=["terminal-bench-2.1-fix-git"],
                          server_mounts=[{"source": "run_tb/socket with spaces", "target": "/run/terminal-bench"}])
        for controller in CONTROLLERS:
            config = controller.parse_run_config(self.config_path, paths=self.paths)
            self.assertEqual(config.environment.mounts,
                             (rf.ServerMount(socket_dir, "/run/terminal-bench"),))

    def test_invalid_mounts_fail_before_launch(self) -> None:
        existing = str(self.directory)
        bad_mounts = [None, "-v /tmp:/tmp", [{}], [{"source": existing}],
                      [{"source": existing, "target": "/data"}],
                      [{"source": existing, "target": "/"}],
                      [{"source": existing, "target": "/./"}],
                      [{"source": existing, "target": "//data"}],
                      [{"source": existing, "target": "relative"}],
                      [{"source": existing, "target": "/run/../data"}],
                      [{"source": existing, "target": "/run/a,ro"}],
                      [{"source": str(self.directory / "missing"), "target": "/run/a"}],
                      [{"source": existing, "target": "/run/a"}] * 2]
        for mounts in bad_mounts:
            with self.subTest(mounts=mounts), self.assertRaises(RuntimeError):
                rf.parse_environment_config({"environment": "example", "server_mounts": mounts}, self.config_path)

    def test_terminal_bench_rejects_a_shared_session_id(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "assigned separately"):
            rf.parse_environment_config({"environment": "terminal_bench",
                                        "environment_parameters": {"session_id": "shared"}}, self.config_path)

    def test_competition_is_no_longer_a_controller_flag(self) -> None:
        for controller in CONTROLLERS:
            self.assertFalse(hasattr(controller.parse_args([]), "competition"))
            with patch("sys.stderr"), self.assertRaises(SystemExit) as error:
                controller.parse_args(["--competition"])
            self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
