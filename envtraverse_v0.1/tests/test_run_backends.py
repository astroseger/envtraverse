from __future__ import annotations

from dataclasses import replace
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import account_locks
import run_backends as rb
import run_controller as controller
import run_funs as rf


class RunBackendTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.paths = rf.RunPaths(self.directory)
        self.config_file = self.directory / "chosen.yaml"
        for backend in (rb.CODEX_CHATGPT, rb.OPENCODE_CHATGPT):
            paths = replace(self.paths, backend=backend)
            for account in ("account_a", "account_b"):
                directory = paths.accounts_dir / account
                directory.mkdir(parents=True)
                if backend == rb.OPENCODE_CHATGPT:
                    (directory / "opencode.db").write_text("synthetic database")
        self.settings = {"games": ["counter"], "environment": "example", "agent": "agent_v1",
                         "accounts": ["account_a"], "agent_parameters": {
                             "model": "gpt-5.6-sol", "reasoning_effort": "max",
                         }}

    def parse(self, **changes):
        self.config_file.write_text(yaml.safe_dump({**self.settings, **changes}))
        return controller.parse_run_config(self.config_file, paths=self.paths)

    def test_backend_defaults_to_legacy_codex(self):
        config = self.parse()
        self.assertEqual(config.backend, rb.CODEX_CHATGPT)
        self.assertEqual(config.paths.accounts_dir, self.directory / "codex_accounts")
        self.assertIsInstance(rb.create_backend(config), rb.CodexChatGPTBackend)

    def test_explicit_backend_selects_paths_and_adapter(self):
        config = self.parse(backend=rb.OPENCODE_CHATGPT, agent="opencode_agent_v1_08_wstop3")
        self.assertEqual(config.paths.accounts_dir, self.directory / "opencode_chatgpt_accounts")
        self.assertIsInstance(rb.create_backend(config), rb.OpenCodeChatGPTBackend)
        config = self.parse(backend=rb.CODEX_API_KEY)
        self.assertEqual(config.accounts, [])
        self.assertIsInstance(rb.create_backend(config), rb.CodexAPIKeyBackend)

    def test_backend_rejects_unknown_and_nonstring_values(self):
        for value in (None, False, [], {}, "codex", ""):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, "backend must be"):
                self.parse(backend=value)

    def test_codex_legacy_account_alias_is_normalized(self):
        self.settings.pop("accounts")
        config = self.parse(codex_accounts=["account_a"])
        self.assertEqual(config.accounts, ["account_a"])
        self.assertEqual(config.parsed_run_config["accounts"], ["account_a"])
        self.assertNotIn("codex_accounts", config.parsed_run_config)

    def test_duplicate_codex_account_keys_rejected_even_when_equal(self):
        for accounts in (["account_a"], ["account_b"]):
            with self.subTest(accounts=accounts), self.assertRaisesRegex(RuntimeError, "accounts"):
                self.parse(codex_accounts=accounts)

    def test_wrong_family_account_alias_is_rejected(self):
        self.settings.pop("accounts")
        for backend, legacy in ((rb.CODEX_CHATGPT, "opencode_accounts"), (rb.OPENCODE_CHATGPT, "codex_accounts")):
            with self.subTest(backend=backend), self.assertRaisesRegex(RuntimeError, legacy):
                self.parse(backend=backend, **{legacy: ["account_a"]})

    def test_account_backend_forbids_parallelism_key_presence(self):
        for backend in (rb.CODEX_CHATGPT, rb.OPENCODE_CHATGPT):
            for value in (None, "all", 1):
                with self.subTest(backend=backend, value=value), self.assertRaisesRegex(RuntimeError, "only valid"):
                    self.parse(backend=backend, max_parallel_games=value)

    def test_api_parallelism_only_accepts_all_or_actual_positive_integer(self):
        for value, expected_count in (("all", 5), (1, 1), (3, 3), (10, 5)):
            config = self.parse(backend=rb.CODEX_API_KEY, max_parallel_games=value)
            self.assertEqual(len(rb.create_backend(config).slots(5)), expected_count)
        for value in (None, True, False, 0, -1, 1.0, "1", "ALL", [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, "positive integer"):
                self.parse(backend=rb.CODEX_API_KEY, max_parallel_games=value)

    def test_api_never_reserves_account_directories(self):
        self.settings.pop("accounts")
        config = self.parse(backend=rb.CODEX_API_KEY)
        adapter = rb.create_backend(config)
        with patch.object(rb, "account_lock") as lock, patch.object(rb, "ensure_account_not_in_use") as guard:
            with adapter.reserve():
                self.assertEqual(adapter.account_locks, {})
            lock.assert_not_called()
            guard.assert_not_called()

    def test_agent_and_backend_families_must_match(self):
        for backend, agent in ((rb.CODEX_CHATGPT, "opencode_agent_v1_08_wstop3"),
                               (rb.CODEX_API_KEY, "opencode_agent_v1_08_wstop3"),
                               (rb.OPENCODE_CHATGPT, "agent_v1")):
            with self.subTest(backend=backend), self.assertRaisesRegex(RuntimeError, "incompatible with backend"):
                self.parse(backend=backend, agent=agent)

    def test_account_pool_locks_last_through_orphan_guard_and_reservation(self):
        for backend, kind, agent in ((rb.CODEX_CHATGPT, "codex", "agent_v1"),
                                     (rb.OPENCODE_CHATGPT, "opencode", "opencode_agent_v1_08_wstop3")):
            with self.subTest(backend=backend):
                config = self.parse(backend=backend, agent=agent, accounts=["account_b", "account_a"])
                adapter = rb.create_backend(config)

                def guard(directory):
                    self.assertEqual(set(adapter.account_locks), {"account_a", "account_b"})
                    with self.assertRaises(account_locks.AccountLockError):
                        with account_locks.account_lock(directory, kind=kind):
                            self.fail("account pool was unlocked during the mount guard")

                with patch.object(rb, "ensure_account_not_in_use", side_effect=guard) as checked:
                    with adapter.reserve():
                        self.assertEqual(adapter.slots(10), ["account_b", "account_a"])
                        descriptors = list(adapter.account_locks.values())
                        self.assertTrue(all(os.fstat(fd) for fd in descriptors))
                    self.assertEqual(checked.call_count, 2)
                self.assertEqual(adapter.account_locks, {})
                for fd in descriptors:
                    with self.assertRaises(OSError):
                        os.fstat(fd)

    def test_orphan_guard_failure_releases_every_account_lock(self):
        config = self.parse(accounts=["account_a", "account_b"])
        adapter = rb.create_backend(config)
        with patch.object(rb, "ensure_account_not_in_use", side_effect=RuntimeError("active container")):
            with self.assertRaisesRegex(RuntimeError, "active container"):
                with adapter.reserve():
                    self.fail("orphan guard did not stop launch")
        self.assertEqual(adapter.account_locks, {})
        for name in config.accounts:
            with account_locks.account_lock(config.paths.accounts_dir / name, kind="codex"):
                pass

    def test_account_guard_failure_creates_no_run_directory_and_releases_pool(self):
        config = self.parse(accounts=["account_a", "account_b"])
        with patch.object(rf, "verify_container_names_available"), \
             patch.object(rf, "verify_network_names_available"), \
             patch.object(rb, "ensure_account_not_in_use", side_effect=RuntimeError("active container")), \
             patch.object(rf, "built_run_images") as build, \
             patch.object(rf, "setup_runner_logging") as logging, \
             patch.object(rf, "make_run_writable") as writable, \
             patch.object(rf.LOGGER, "exception"), patch("sys.stderr", new_callable=io.StringIO) as errors:
            result = controller.main(["--run-root", str(self.paths.root), "--run-config", str(self.config_file)])
        self.assertEqual(result, 1)
        self.assertIn("active container", errors.getvalue())
        self.assertFalse(self.paths.run_dir.exists())
        build.assert_not_called()
        logging.assert_not_called()
        writable.assert_not_called()
        for name in config.accounts:
            with account_locks.account_lock(config.paths.accounts_dir / name, kind="codex"):
                pass

    def test_pending_opencode_usage_blocks_before_docker_guard(self):
        config = self.parse(backend=rb.OPENCODE_CHATGPT, agent="opencode_agent_v1_08_wstop3")
        adapter = rb.create_backend(config)
        marker = rb.pending_usage_path("account_a", paths=config.paths)
        marker.write_text("synthetic pending usage")
        with patch.object(rb, "ensure_account_not_in_use") as guard:
            with self.assertRaisesRegex(RuntimeError, "unsaved game usage"):
                with adapter.reserve():
                    self.fail("pending usage allowed account reuse")
            guard.assert_not_called()
        self.assertTrue(marker.exists())
        self.assertEqual(adapter.account_locks, {})

    def test_codex_check_and_clean_share_reserved_descriptor(self):
        config = self.parse()
        adapter = rb.create_backend(config)
        process = Mock(pid=123456)
        process.wait.return_value = 0
        with patch.object(rb, "ensure_account_not_in_use"), \
             patch.object(rb.subprocess, "run") as command, \
             patch.object(rb.subprocess, "Popen", return_value=process) as launch:
            with adapter.reserve():
                fd = adapter.account_locks["account_a"]
                adapter.preflight("unused-agent-image")
                adapter.prepare_game("account_a", "game0", "counter", "container", "unused-agent-image")
                self.assertEqual(command.call_count, 1)
                self.assertEqual(launch.call_count, 1)
                for call, script in ((launch.call_args, rb.CHECK_CODEX_ACCOUNT_SCRIPT),
                                     (command.call_args, rb.CLEAN_CODEX_ACCOUNT_SCRIPT)):
                    self.assertEqual(call.args[0], ["bash", str(script), str(config.paths.accounts_dir / "account_a"),
                                                   "--lock-fd", str(fd)])
                    self.assertEqual(call.kwargs["pass_fds"], (fd,))
                    self.assertTrue(call.kwargs["start_new_session"])
                self.assertTrue(command.call_args.kwargs["check"])
                process.wait.assert_called_once_with()

    def test_reserved_live_check_stops_process_group_and_waits_before_unlock(self):
        config = self.parse()
        adapter = rb.create_backend(config)
        process = Mock(pid=123456)
        events = []

        def wait_for_cleanup():
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            events.append("helper-cleanup")
            self.assertTrue(adapter.account_locks)
            return 143

        process.wait.side_effect = wait_for_cleanup
        original = signal.getsignal(signal.SIGINT)
        with patch.object(rb, "ensure_account_not_in_use"), \
             patch.object(rb.subprocess, "Popen", return_value=process), \
             patch.object(rb.os, "killpg", side_effect=lambda *args: events.append("terminate")) as stop:
            with self.assertRaises(KeyboardInterrupt):
                with adapter.reserve():
                    adapter.preflight("unused-agent-image")
            stop.assert_called_once_with(process.pid, signal.SIGTERM)
        self.assertEqual(events, ["terminate", "helper-cleanup"])
        self.assertEqual(adapter.account_locks, {})
        self.assertIs(signal.getsignal(signal.SIGINT), original)

    def test_reserved_live_check_honors_signal_during_spawn(self):
        process = Mock(pid=123456)
        process.wait.return_value = 0

        def spawn(*args, **kwargs):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return process

        with patch.object(rb.subprocess, "Popen", side_effect=spawn), patch.object(rb.os, "killpg") as stop:
            with self.assertRaises(KeyboardInterrupt):
                rb.run_reserved_live_command(["synthetic-helper"], 99)
            stop.assert_called_once_with(process.pid, signal.SIGTERM)
            process.wait.assert_called_once_with()

    def test_reserved_helper_preserves_stop_intent_after_spawn_failure(self):
        def stopped_command(*args, **kwargs):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            raise OSError("synthetic spawn failure")
        original_handler = signal.getsignal(signal.SIGTERM)
        with patch.object(rb.subprocess, "run", side_effect=stopped_command):
            with self.assertRaises(KeyboardInterrupt):
                rb.run_reserved_command(["synthetic-helper"], 99)
        self.assertIs(signal.getsignal(signal.SIGTERM), original_handler)

    def test_api_secret_is_only_in_process_environment(self):
        adapter = rb.create_backend(self.parse(backend=rb.CODEX_API_KEY))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-secret"}):
            preparation = rf.LaunchPreparation(command=["synthetic-agent"], mounts=[])
            command = controller.docker_agent_command("api-worker-0", "container", "network", "server", "proxy",
                                                      preparation, agent_image="image", backend=adapter)
            self.assertIn("CODEX_API_KEY", command)
            self.assertNotIn("synthetic-secret", " ".join(command))
            self.assertEqual(adapter.process_environment()["CODEX_API_KEY"], "synthetic-secret")
            self.assertFalse(adapter.log_command)


if __name__ == "__main__":
    unittest.main()
