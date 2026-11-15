from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.controller import run_backends as rb
import run_controller as controller
from src.controller import run_funs as rf


LEGACY_AGENT_PARAMETERS = {
    "model": "gpt-5.6-sol",
    "reasoning_effort": "max",
    "http_read_timeout_seconds": 1800,
    "supervisor_inactivity_timeout_seconds": 3600,
    "recovery_delay_seconds": 2.5,
    "max_recovery_attempts": 4,
    "codex_account_fast_service_tier": True,
}
OPENCODE_PARAMETERS = {
    "opencode_thinking": True,
    "opencode_agent": "build",
    "opencode_shell_timeout_background": False,
}


class AgentParameterMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.config_path = self.directory / "run.yaml"
        self.paths = rf.RunPaths(self.directory)
        (self.directory / "codex_accounts/test_account").mkdir(parents=True)
        oc_account = self.directory / "opencode_chatgpt_accounts/test_account"
        oc_account.mkdir(parents=True)
        (oc_account / "opencode.db").touch()

    def normalize(self, settings):
        backend = controller.select_backend(settings)
        return controller.normalize_run_config(settings, backend=backend, path=self.config_path)

    def parse(self, settings):
        self.config_path.write_text(yaml.safe_dump(settings), encoding="utf-8")
        return controller.parse_run_config(self.config_path, paths=self.paths)

    def base_config(self, backend=rb.CODEX_CHATGPT):
        settings = {
            "backend": backend,
            "agent": "opencode_agent_v1_08_wstop3" if backend == rb.OPENCODE_CHATGPT else "agent_v1_08_wstop3",
            "environment": "example",
            "games": ["counter"],
            "agent_parameters": {"model": "gpt-5.6-sol", "reasoning_effort": "max"},
        }
        if backend != rb.CODEX_API_KEY:
            settings["accounts"] = ["test_account"]
        return settings

    def test_legacy_codex_and_canonical_configs_have_identical_effective_configuration(self) -> None:
        canonical = self.base_config()
        canonical["agent_parameters"] = deepcopy(LEGACY_AGENT_PARAMETERS)
        canonical.update({
            "runner": {"container_memory_gib": 8},
            "agent_docker_build_args": {"CODEX_VERSION": "0.166.0"},
            "environment_parameters": {"target": 3},
        })
        legacy = deepcopy(canonical)
        legacy["codex_accounts"] = legacy.pop("accounts")
        legacy.update(legacy.pop("agent_parameters"))
        expected = self.parse(canonical)
        for explicit_backend in (True, False):
            supplied = deepcopy(legacy)
            if not explicit_backend:
                supplied.pop("backend")
            with self.subTest(explicit_backend=explicit_backend):
                actual = self.parse(supplied)
                self.assertEqual(actual, expected)
                self.assertEqual(actual.backend, rb.CODEX_CHATGPT)
                self.assertEqual(actual.agent_parameters, LEGACY_AGENT_PARAMETERS)
                self.assertNotIn("codex_accounts", actual.parsed_run_config)
                for key in LEGACY_AGENT_PARAMETERS:
                    self.assertNotIn(key, actual.parsed_run_config)

    def test_mixed_legacy_and_nested_codex_parameters_combine_without_mutating_input(self) -> None:
        settings = {
            "codex_accounts": ["test_account"],
            "model": "gpt-5.6-sol",
            "recovery_delay_seconds": 1,
            "agent_parameters": {"reasoning_effort": "max", "custom": {"values": [None, True, 3]}},
        }
        original = deepcopy(settings)
        normalized = self.normalize(settings)
        self.assertEqual(normalized, {
            "backend": rb.CODEX_CHATGPT,
            "accounts": ["test_account"],
            "agent_parameters": {
                "model": "gpt-5.6-sol", "reasoning_effort": "max",
                "recovery_delay_seconds": 1, "custom": {"values": [None, True, 3]},
            },
        })
        self.assertEqual(settings, original)

    def test_duplicate_legacy_and_nested_parameters_are_rejected_even_when_equal(self) -> None:
        for key, value in LEGACY_AGENT_PARAMETERS.items():
            for nested in (value, "a different value"):
                with self.subTest(key=key, nested=nested), self.assertRaisesRegex(
                    RuntimeError, rf"duplicate configuration.*{key}"
                ):
                    self.normalize({key: value, "agent_parameters": {key: nested}})

    def test_duplicate_account_forms_are_rejected_even_when_equal(self) -> None:
        for accounts in (["test_account"], ["different_account"]):
            with self.subTest(accounts=accounts), self.assertRaisesRegex(RuntimeError, "duplicate configuration"):
                self.normalize({"codex_accounts": ["test_account"], "accounts": accounts})

    def test_empty_parameters_do_not_insert_agent_defaults(self) -> None:
        for backend in sorted(rb.BACKENDS):
            for supplied in ({}, {"agent_parameters": {}}):
                with self.subTest(backend=backend, supplied=supplied):
                    self.assertEqual(self.normalize({"backend": backend, **supplied}), {
                        "backend": backend, "agent_parameters": {},
                    })

    def test_normalization_is_idempotent(self) -> None:
        examples = [
            {"codex_accounts": ["test_account"], **LEGACY_AGENT_PARAMETERS},
            *({"backend": backend, "agent_parameters": {"custom": {"values": [1, None, "text"]}}}
              for backend in sorted(rb.BACKENDS)),
        ]
        for settings in examples:
            with self.subTest(settings=settings):
                normalized = self.normalize(settings)
                self.assertEqual(self.normalize(normalized), normalized)

    def test_agent_parameters_must_be_a_mapping(self) -> None:
        for backend in sorted(rb.BACKENDS):
            for value in (None, False, [], ["model"], "model: example", 1):
                with self.subTest(backend=backend, value=value), self.assertRaisesRegex(
                    RuntimeError, "agent_parameters.*must be a mapping"
                ):
                    self.normalize({"backend": backend, "agent_parameters": value})

    def test_custom_nested_parameters_and_structured_models_are_not_globally_interpreted(self) -> None:
        parameters = {
            "model": {"custom": "format"},
            "custom_value": "kept",
            "custom_options": {"values": [None, True, 3, 1.5, "こんにちは"], "empty": {}},
        }
        for backend in sorted(rb.BACKENDS):
            with self.subTest(backend=backend):
                self.assertEqual(self.normalize({"backend": backend, "agent_parameters": parameters}), {
                    "backend": backend, "agent_parameters": parameters,
                })

    def test_non_codex_chatgpt_backends_reject_every_legacy_agent_root_key(self) -> None:
        for backend in (rb.OPENCODE_CHATGPT, rb.CODEX_API_KEY):
            for key, value in LEGACY_AGENT_PARAMETERS.items():
                with self.subTest(backend=backend, key=key), self.assertRaisesRegex(
                    RuntimeError, rf"agent_parameters\.{key}"
                ):
                    self.normalize({"backend": backend, key: value})

    def test_non_codex_chatgpt_backends_reject_both_account_aliases(self) -> None:
        for backend in (rb.OPENCODE_CHATGPT, rb.CODEX_API_KEY):
            for key in ("codex_accounts", "opencode_accounts"):
                with self.subTest(backend=backend, key=key), self.assertRaisesRegex(RuntimeError, rf"{key}.*use accounts"):
                    self.normalize({"backend": backend, key: ["test_account"]})

    def test_codex_chatgpt_rejects_opencode_account_alias(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "opencode_accounts.*use accounts"):
            self.normalize({"opencode_accounts": ["test_account"]})

    def test_opencode_options_at_root_are_rejected_on_every_backend(self) -> None:
        for backend in sorted(rb.BACKENDS):
            for key, value in OPENCODE_PARAMETERS.items():
                with self.subTest(backend=backend, key=key), self.assertRaisesRegex(
                    RuntimeError, rf"agent_parameters\.{key}"
                ):
                    self.normalize({"backend": backend, key: value})

    def test_explicit_non_codex_backends_reject_legacy_fields_during_parsing(self) -> None:
        for backend in (rb.OPENCODE_CHATGPT, rb.CODEX_API_KEY):
            for key, value in {**LEGACY_AGENT_PARAMETERS, "codex_accounts": ["test_account"]}.items():
                settings = self.base_config(backend)
                settings[key] = value
                with self.subTest(backend=backend, key=key), self.assertRaisesRegex(RuntimeError, key):
                    self.parse(settings)

    def test_canonical_configs_select_backend_from_yaml(self) -> None:
        for backend in sorted(rb.BACKENDS):
            settings = self.base_config(backend)
            with self.subTest(backend=backend):
                config = self.parse(settings)
                self.assertEqual(config.backend, backend)
                self.assertEqual(config.parsed_run_config["backend"], backend)
                self.assertIs(config.agent_parameters, config.parsed_run_config["agent_parameters"])
                self.assertEqual(config.agent_parameters, settings["agent_parameters"])

    def test_opencode_preflight_uses_nested_model_and_reasoning_effort(self) -> None:
        settings = self.base_config(rb.OPENCODE_CHATGPT)
        settings["agent_parameters"].update({
            "model": "openai/gpt-6.1-terra", "reasoning_effort": "max", **OPENCODE_PARAMETERS,
        })
        config = self.parse(settings)
        with patch.object(rb.account_tools, "Docker") as docker, patch.object(rb.account_tools, "validate_ping") as validate:
            docker.return_value.run.return_value = "synthetic PING result"
            rb.verify_opencode_accounts(config, agent_image="test-image")
        args = docker.return_value.run.call_args.args
        self.assertEqual(args[0], self.directory / "opencode_chatgpt_accounts/test_account")
        command = args[1]
        self.assertEqual(command[command.index("--model") + 1], "openai/gpt-6.1-terra#max")
        validate.assert_called_once_with("synthetic PING result")

    def test_codex_game_cleanup_uses_nested_fast_service_tier(self) -> None:
        settings = self.base_config()
        settings["agent_parameters"]["codex_account_fast_service_tier"] = True
        config = self.parse(settings)
        backend = rb.create_backend(config)
        backend.account_locks["test_account"] = 123
        with patch.object(rb, "clean_codex_account") as clean:
            backend.prepare_game("test_account", "alias", "counter", "container", "image")
        clean.assert_called_once_with("test_account", account_lock_fd=123, fast_service_tier=True, paths=config.paths)


if __name__ == "__main__":
    unittest.main()
