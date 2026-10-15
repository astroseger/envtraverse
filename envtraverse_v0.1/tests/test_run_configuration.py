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
        accounts = self.directory / "accounts"
        (accounts / "codex01").mkdir(parents=True)
        self.enterContext(patch.object(rf, "CODEX_ACCOUNTS_DIR", accounts))

    def write_config(self, **settings):
        config = {"agent": "agent_v1", "environment": "example", "games": ["counter"], "codex_accounts": ["codex01"]}
        config.update(settings)
        self.config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
        return config

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
                        config = controller.parse_run_config(self.config_path)
                        self.assertEqual(config.environment, rf.EnvironmentConfig(name, parameters, []))
                        self.assertEqual(config.games, [game])

    def test_supplied_config_runs_one_noncompetition_arc3_game(self) -> None:
        for controller in CONTROLLERS:
            config = controller.parse_run_config(ROOT / "run_config_arc3.yaml")
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
                config = controller.parse_run_config(self.config_path)
                self.assertEqual(config.environment, rf.EnvironmentConfig("example", {}, []))
                rf.verify_environment_variables(config.environment)

    def test_account_fast_service_tier_defaults_to_false_and_accepts_booleans(self) -> None:
        self.write_config()
        self.assertIs(accounts_controller.parse_run_config(self.config_path).codex_account_fast_service_tier, False)
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.write_config(codex_account_fast_service_tier=enabled)
                config = accounts_controller.parse_run_config(self.config_path)
                self.assertIs(config.codex_account_fast_service_tier, enabled)

    def test_account_fast_service_tier_rejects_non_booleans(self) -> None:
        for value in (None, 0, 1, "true", "false", [], {}):
            with self.subTest(value=value):
                self.write_config(codex_account_fast_service_tier=value)
                with self.assertRaisesRegex(RuntimeError, "codex_account_fast_service_tier.*must be a boolean"):
                    accounts_controller.parse_run_config(self.config_path)

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
                        controller.parse_run_config(self.config_path)

    def test_duplicate_games_remain_rejected(self) -> None:
        self.write_config(games=["counter", "counter"])
        for controller in CONTROLLERS:
            with self.assertRaisesRegex(RuntimeError, "games must be unique"):
                controller.parse_run_config(self.config_path)

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

    def test_both_controllers_resolve_server_mounts_from_project_root(self) -> None:
        socket_dir = self.directory / "run_tb" / "socket with spaces"
        socket_dir.mkdir(parents=True)
        self.write_config(environment="terminal_bench", games=["terminal-bench-2.1-fix-git"],
                          server_mounts=[{"source": "run_tb/socket with spaces", "target": "/run/terminal-bench"}])
        with patch.object(rf, "ROOT", self.directory):
            for controller in CONTROLLERS:
                config = controller.parse_run_config(self.config_path)
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
