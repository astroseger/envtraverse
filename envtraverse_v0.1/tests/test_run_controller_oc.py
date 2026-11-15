from __future__ import annotations

from contextlib import contextmanager
import csv
import json
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

import run_controller as controller
from src.controller import run_backends as backends
from src.controller import run_funs as rf


class OpenCodeControllerTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.paths = rf.RunPaths(self.directory / "experiment", self.directory / "original accounts,with comma",
                                 backend=backends.OPENCODE_CHATGPT)
        self.account = self.paths.accounts_dir / "oc_test"
        self.account.mkdir(parents=True)
        (self.account / "opencode.db").write_text("synthetic database")
        self.config_file = self.directory / "config.yaml"
        agents = self.directory / "agents"
        agent = agents / "opencode_test"
        agent.mkdir(parents=True)
        (agent / "Dockerfile").write_text("FROM scratch\nARG OPENCODE_VERSION=2.3.0\n")
        (agent / "prepare_run.py").touch()
        codex_agent = agents / "agent_v1"
        codex_agent.mkdir()
        (codex_agent / "Dockerfile").write_text("FROM scratch\n")
        (codex_agent / "prepare_run.py").touch()
        self.enterContext(patch.object(rf, "AGENTS_DIR", agents))
        self.config = {"backend": backends.OPENCODE_CHATGPT, "agent": "opencode_test", "environment": "example", "games": ["counter"],
                       "accounts": ["oc_test"],
                       "agent_parameters": {"model": "gpt-5.6-sol", "reasoning_effort": "max"}}

    def parse(self, **changes):
        config = {**self.config, **changes}
        self.config_file.write_text(yaml.safe_dump(config))
        return controller.parse_run_config(self.config_file, paths=self.paths)

    def backend(self, config=None, locks=None):
        backend = backends.create_backend(config or self.parse())
        backend.account_locks = locks or {}
        return backend

    def simulate_games(self, mode="normal", games=None):
        """Run the real scheduler/launcher with only Docker and usage I/O mocked."""
        games = games or ["counter"]
        config = self.parse(games=games)
        self.paths.run_dir.mkdir(parents=True, exist_ok=True)
        names = rf.build_container_names(games)
        images = rf.RunImages("agent-image", "server-image", "proxy-image")
        self.events = []
        marker = backends.pending_usage_path("oc_test", paths=self.paths)
        proxy = Mock()
        proxy.poll.side_effect = [None, 1] if mode == "proxy_failure" else None
        proxy.poll.return_value = None
        server = Mock()
        server.poll.side_effect = [None, 1] if mode == "server_failure" else None
        server.poll.return_value = None

        def prepare(*args, **kwargs):
            self.assertEqual(args[4], config.agent_parameters)
            self.assertNotIn("accounts", args[4])
            self.assertNotIn("environment", args[4])
            if mode == "prepare_failure":
                raise RuntimeError("synthetic preparation failure")
            (args[1] / "run").mkdir()
            return rf.LaunchPreparation(command=["synthetic-agent"], mounts=[])

        def clean(*args, **kwargs):
            self.assertFalse(marker.exists(), "The prior game's usage must be saved before cleanup")
            self.events.append("clean")
            if mode == "clean_failure":
                raise subprocess.CalledProcessError(1, "clean")
            if mode == "interrupt_cleanup":
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        def launch(*args, **kwargs):
            self.events.append("launch")
            data = json.loads(marker.read_text())
            self.assertEqual(data["account"], str(self.account))
            self.assertEqual(data["image"], images.agent)
            self.assertIn(data["container_name"], names.agents.values())
            if mode == "launch_failure":
                raise OSError("synthetic agent launch failure")
            process = Mock()
            process.poll.return_value = 0
            if mode == "shutdown":
                process.poll.return_value = None
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return process

        def stop(game):
            self.events.append(("stop", game.alias))

        def collect(account, output, **kwargs):
            alias = kwargs["game_alias"]
            self.events.append(("collect", alias))
            self.assertIn(("stop", alias), self.events)
            self.assertEqual(account, self.account)
            self.assertEqual(output, self.paths.run_dir / alias / "run/opencode_usage.json")
            self.assertTrue(marker.exists())
            with self.assertRaises(backends.account_tools.AccountError):
                with backends.account_tools.account_lock(account):
                    self.fail("Snapshot collection released the account lock")
            if mode == "collect_failure":
                raise RuntimeError("synthetic collection failure")
            output.write_text('{"source": "synthetic account-wide usage"}\n')
            return {}

        with rf.lock_accounts(["oc_test"], paths=self.paths) as locks, \
             patch.object(rf, "create_network"), patch.object(rf, "remove_network"), \
             patch.object(rf, "start_proxy", return_value=rf.RunningContainer("proxy", proxy)), \
             patch.object(rf, "start_server", return_value=rf.RunningContainer("server", server)), \
             patch.object(rf, "health_check_ok", return_value=True), \
             patch.object(rf, "prepare_agent_run", side_effect=prepare), \
             patch.object(rf, "stop_game", side_effect=stop), patch.object(rf, "stop_container"), \
             patch.object(rf.time, "sleep"), patch.object(rf, "log_info"), patch.object(rf.LOGGER, "exception"), \
             patch.object(controller.subprocess, "run", side_effect=clean), \
             patch.object(controller.subprocess, "Popen", side_effect=launch), \
             patch.object(backends, "collect_account_usage", side_effect=collect):
            return controller.run_scheduler(config, names, {f"game{i}": game for i, game in enumerate(games)},
                                            images=images, backend=self.backend(config, locks))

    def test_usage_snapshot_precedes_next_cleanup_and_covers_last_game(self):
        self.assertEqual(self.simulate_games(games=["counter", "second"]), 0)
        self.assertEqual(self.events, ["clean", "launch", ("stop", "game0"), ("collect", "game0"),
                                       "clean", "launch", ("stop", "game1"), ("collect", "game1")])
        for alias in ("game0", "game1"):
            self.assertTrue((self.paths.run_dir / alias / "run/opencode_usage.json").is_file())
        self.assertFalse(backends.pending_usage_path("oc_test", paths=self.paths).exists())

    def test_usage_is_saved_on_graceful_shutdown(self):
        self.assertEqual(self.simulate_games("shutdown"), 130)
        self.assertEqual(self.events[-2:], [("stop", "game0"), ("collect", "game0")])
        self.assertFalse(backends.pending_usage_path("oc_test", paths=self.paths).exists())

    def test_usage_is_saved_after_proxy_failure(self):
        self.assertEqual(self.simulate_games("proxy_failure"), 1)
        self.assertEqual(self.events[-2:], [("stop", "game0"), ("collect", "game0")])

    def test_usage_is_saved_after_server_failure(self):
        self.assertEqual(self.simulate_games("server_failure"), 1)
        self.assertEqual(self.events[-2:], [("stop", "game0"), ("collect", "game0")])

    def test_launch_failure_after_cleanup_still_saves_usage(self):
        self.assertEqual(self.simulate_games("launch_failure"), 1)
        self.assertEqual(self.events, ["clean", "launch", ("stop", "game0"), ("collect", "game0")])

    def test_interrupted_successful_cleanup_registers_and_finalizes_empty_game(self):
        with self.assertRaises(KeyboardInterrupt):
            self.simulate_games("interrupt_cleanup")
        self.assertEqual(self.events, ["clean", ("stop", "game0"), ("collect", "game0")])
        self.assertFalse(backends.pending_usage_path("oc_test", paths=self.paths).exists())

    def test_preparation_failure_does_not_collect_previous_database(self):
        self.assertEqual(self.simulate_games("prepare_failure"), 1)
        self.assertEqual(self.events, [("stop", "game0")])

    def test_cleanup_failure_does_not_collect_previous_database(self):
        self.assertEqual(self.simulate_games("clean_failure"), 1)
        self.assertEqual(self.events, ["clean", ("stop", "game0")])

    def test_collection_failure_preserves_pending_marker_and_blocks_next_game(self):
        with self.assertRaisesRegex(RuntimeError, "was preserved with pending marker"):
            self.simulate_games("collect_failure", games=["counter", "second"])
        self.assertEqual(self.events, ["clean", "launch", ("stop", "game0"), ("collect", "game0")])
        marker = backends.pending_usage_path("oc_test", paths=self.paths)
        self.assertEqual(json.loads(marker.read_text())["game_alias"], "game0")
        with patch.object(controller.subprocess, "run") as clean:
            with self.assertRaisesRegex(RuntimeError, "unsaved game usage"):
                backends.clean_opencode_account("oc_test", account_lock_fd=123,
                                                  agent_image="agent-image", paths=self.paths)
            clean.assert_not_called()

    def test_pending_marker_blocks_new_controller_before_preflight_or_build(self):
        config = self.parse()
        marker = backends.pending_usage_path("oc_test", paths=self.paths)
        marker.write_text("partial marker left by abrupt shutdown")
        for name in ("verify_environment_variables", "verify_container_names_available", "verify_network_names_available",
                     "prepare_run_root", "setup_runner_logging", "make_run_writable"):
            self.enterContext(patch.object(rf, name))
        with patch.object(controller, "parse_run_config", return_value=config), \
             patch.object(rf, "built_run_images") as build, \
             patch.object(backends, "verify_opencode_accounts") as check:
            self.assertEqual(controller.main(["--run-root", str(self.paths.root),
                                              "--accounts-dir", str(self.paths.accounts_dir)]), 1)
            build.assert_not_called()
            check.assert_not_called()
        self.assertTrue(marker.exists())

    def test_shutdown_stops_all_writers_before_collecting_and_continues_after_collection_failure(self):
        events = []
        process = Mock()
        games = {slot: rf.RunningGame(slot, slot, rf.RunningContainer(slot, process), 0)
                 for slot in ("first", "second")}
        def stop(game):
            events.append(("stop", game.alias))
        def collect(slot, game):
            self.assertIn(("stop", "first"), events)
            self.assertIn(("stop", "second"), events)
            events.append(("collect", slot))
            if slot == "first":
                raise RuntimeError("synthetic first account collection failure")
        with patch.object(rf, "stop_game", side_effect=stop):
            with self.assertRaisesRegex(RuntimeError, "first account"):
                rf.stop_all(games, None, finalize_game=collect)
        self.assertEqual(events, [("stop", "first"), ("stop", "second"),
                                  ("collect", "first"), ("collect", "second")])
        self.assertFalse(games)

    def test_failed_container_stop_does_not_finalize_its_usage(self):
        process = Mock()
        games = {slot: rf.RunningGame(slot, slot, rf.RunningContainer(slot, process), 0)
                 for slot in ("first", "second")}
        def stop(game):
            if game.alias == "first":
                raise RuntimeError("synthetic stop failure")
        second = games["second"]
        collect = Mock()
        with patch.object(rf, "stop_game", side_effect=stop):
            with self.assertRaisesRegex(RuntimeError, "stop failure"):
                rf.stop_all(games, None, finalize_game=collect)
        collect.assert_called_once_with("second", second)

    def test_shared_paths_explicit_config_and_original_accounts_override(self):
        args = controller.parse_args(["--run-config", str(ROOT / "run_config_examples/run_config_opencode.yaml")])
        paths = rf.RunPaths(args.run_root, args.accounts_dir, backend=backends.OPENCODE_CHATGPT)
        self.assertEqual(paths.run_config, Path.cwd() / "run_config.yaml")
        self.assertEqual(paths.resolve_config(args.run_config), ROOT / "run_config_examples/run_config_opencode.yaml")
        self.assertEqual(paths.accounts_dir, Path.cwd() / "opencode_chatgpt_accounts")
        self.assertEqual(paths.run_dir, Path.cwd() / "run")
        self.assertEqual(self.paths.accounts_dir, self.directory / "original accounts,with comma")
        self.assertEqual(self.paths.run_dir, self.directory / "experiment/run")
        self.assertEqual(self.parse().accounts, ["oc_test"])

    def test_model_mapping_requires_separate_model_and_effort(self):
        self.assertEqual(rf.resolve_model(self.config["agent_parameters"], self.config_file), "openai/gpt-5.6-sol#max")
        self.assertEqual(rf.resolve_model({"model": "openai/gpt-6.1-sol", "reasoning_effort": "high"},
                                         self.config_file),
                         "openai/gpt-6.1-sol#high")
        for changes in ({"model": "other/model"}, {"model": "openai/"}, {"model": "openai/model#"},
                        {"model": "openai/model#high"}, {"reasoning_effort": "high#max"},
                        {"reasoning_effort": "high/max"}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                self.parse(agent_parameters={**self.config["agent_parameters"], **changes})

    def test_config_rejects_missing_or_invalid_model_and_effort(self):
        for key in ("model", "reasoning_effort"):
            missing = {**self.config, "agent_parameters": dict(self.config["agent_parameters"])}
            del missing["agent_parameters"][key]
            self.config_file.write_text(yaml.safe_dump(missing))
            with self.subTest(key=key, value="omitted"), self.assertRaisesRegex(RuntimeError, key + " is required"):
                controller.parse_run_config(self.config_file, paths=self.paths)
            for value in (None, "", " ", "value with spaces", 42, True, []):
                with self.subTest(key=key, value=value), self.assertRaisesRegex(RuntimeError, key + " is required"):
                    self.parse(agent_parameters={**self.config["agent_parameters"], key: value})

    def test_only_opencode_agents_and_build_arguments_are_accepted(self):
        with self.assertRaisesRegex(RuntimeError, "incompatible"):
            self.parse(agent="agent_v1")
        with self.assertRaisesRegex(RuntimeError, "does not declare"):
            self.parse(agent_docker_build_args={"CODEX_VERSION": "0.154.0"})
        parsed = self.parse(agent_docker_build_args={"OPENCODE_VERSION": "2.3.0"})
        self.assertEqual(parsed.agent_docker_build_args, {"OPENCODE_VERSION": "2.3.0"})

    def test_duplicate_traversal_symlink_and_missing_database_accounts_are_rejected(self):
        for accounts in (["oc_test", "oc_test"], ["../oc_test"], ["/tmp/account"]):
            with self.subTest(accounts=accounts), self.assertRaises(RuntimeError):
                self.parse(accounts=accounts)
        (self.paths.accounts_dir / "linked").symlink_to(self.account)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            self.parse(accounts=["linked"])
        (self.account / "opencode.db").unlink()
        with self.assertRaisesRegex(RuntimeError, "database"):
            self.parse()

    def test_lock_matches_account_tools_and_prevents_concurrent_access(self):
        lock_path = self.paths.accounts_dir / ".oc_test.opencode.lock"
        with rf.lock_accounts(["oc_test"], paths=self.paths) as locks:
            self.assertEqual(set(locks), {"oc_test"})
            self.assertEqual(os.fstat(locks["oc_test"]).st_ino, lock_path.stat().st_ino)
            with self.assertRaises(backends.account_tools.AccountError):
                with backends.account_tools.account_lock(self.account):
                    self.fail("Account helper obtained a controller-owned lock")
            with self.assertRaisesRegex(RuntimeError, "[Aa]ccount is in use"):
                with rf.lock_accounts(["oc_test"], paths=self.paths):
                    self.fail("Second controller obtained the same lock")
        self.assertTrue(lock_path.exists())
        with backends.account_tools.account_lock(self.account):
            pass
        self.assertEqual(lock_path.stat().st_mode & 0o777, 0o600)

    def test_partial_lock_acquisition_failure_releases_previously_acquired_locks(self):
        second = self.paths.accounts_dir / "zz_busy"
        second.mkdir()
        (second / "opencode.db").touch()
        with backends.account_tools.account_lock(second):
            with self.assertRaises(RuntimeError):
                with rf.lock_accounts(["oc_test", "zz_busy"], paths=self.paths):
                    self.fail("Locked busy account")
            with backends.account_tools.account_lock(self.account):
                pass

    def test_agent_bind_mount_uses_original_account_without_forwarding_api_keys(self):
        preparation = rf.LaunchPreparation(command=["python3", "agent_runner.py", "counter"], mounts=[])
        command = controller.docker_agent_command("oc_test", "agent", "internal", "server", "proxy",
                                                 preparation, agent_image="oc-image", backend=self.backend())
        fields = next(csv.reader([command[command.index("--mount") + 1]]))
        mount = dict(field.split("=", 1) for field in fields)
        self.assertEqual(mount["source"], str(self.account))
        self.assertEqual(mount["target"], "/home/user/.local/share/opencode")
        self.assertIn("--init", command)
        self.assertIn("--pull=never", command)
        self.assertIn("HTTPS_PROXY=http://proxy:3128", command)
        self.assertNotIn("OPENAI_API_KEY", " ".join(command))
        self.assertNotIn(".codex", " ".join(command))

    def test_live_check_mounts_original_account_and_never_exports_or_cleans(self):
        config = self.parse()
        with patch.object(backends.account_tools, "Docker") as docker_class, \
             patch.object(backends.account_tools, "export_credentials") as export, \
             patch.object(backends.account_tools, "clean") as clean:
            docker_class.return_value.run.return_value = b'{"type":"text","part":{"text":"PING"}}\n'
            backends.verify_opencode_accounts(config, agent_image="oc-image")
            args, kwargs = docker_class.return_value.run.call_args
            self.assertEqual(args[0], self.account)
            self.assertIn("openai/gpt-5.6-sol#max", args[1])
            self.assertEqual(args[1][-1], backends.account_tools.PING_PROMPT)
            self.assertIn("--standalone", args[1])
            self.assertEqual(kwargs["operation"], "OpenCode account live check")
            export.assert_not_called()
            clean.assert_not_called()
        self.assertEqual((self.account / "opencode.db").read_text(), "synthetic database")

    def test_every_fresh_game_cleans_before_launch_with_original_account_mount(self):
        preparation = rf.LaunchPreparation(command=["python3", "agent_runner.py", "counter"], mounts=[])
        sequence = Mock()
        with rf.lock_accounts(["oc_test"], paths=self.paths) as locks, \
             patch.object(controller.subprocess, "run") as run, \
             patch.object(controller.subprocess, "Popen") as popen, \
             patch.object(backends.account_tools, "export_credentials") as export, \
             patch.object(backends, "register_game_usage"):
            sequence.attach_mock(run, "clean")
            sequence.attach_mock(popen, "launch")
            for game in ("counter", "second"):
                result = controller.start_agent("oc_test", game, game, "agent", "internal", "server",
                                                "proxy", preparation, backend=self.backend(locks=locks),
                                                agent_image="oc-image", check_cancelled=lambda: None)
                self.assertIs(result.process, popen.return_value)
            self.assertEqual([call[0] for call in sequence.mock_calls], ["clean", "launch", "clean", "launch"])
            for call in run.call_args_list:
                self.assertEqual(call.args[0], ["bash", str(backends.CLEAN_OPENCODE_ACCOUNT_SCRIPT),
                                               str(self.account), "--lock-fd", str(locks["oc_test"]),
                                               "--image", "oc-image"])
                self.assertEqual(call.kwargs["pass_fds"], (locks["oc_test"],))
                self.assertTrue(call.kwargs["check"])
                self.assertTrue(call.kwargs["start_new_session"])
            mount_fields = next(csv.reader([popen.call_args.args[0][popen.call_args.args[0].index("--mount") + 1]]))
            self.assertIn("source=" + str(self.account), mount_fields)
            # The controller delegates the offline reset; no credential export
            # or copying occurs in the online game launch itself.
            export.assert_not_called()
        self.assertEqual((self.account / "opencode.db").read_text(), "synthetic database")

    def test_cleaner_failure_prevents_agent_launch(self):
        preparation = rf.LaunchPreparation(command=["python3", "agent_runner.py", "counter"], mounts=[])
        with rf.lock_accounts(["oc_test"], paths=self.paths) as locks, \
             patch.object(controller.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "clean")), \
             patch.object(controller.subprocess, "Popen") as popen:
            with self.assertRaises(subprocess.CalledProcessError):
                controller.start_agent("oc_test", "game0", "counter", "agent", "internal", "server",
                                       "proxy", preparation, backend=self.backend(locks=locks),
                                       agent_image="oc-image", check_cancelled=lambda: None)
            popen.assert_not_called()

    def test_shutdown_waits_for_cleaner_and_prevents_agent_launch(self):
        preparation = rf.LaunchPreparation(command=["python3", "agent_runner.py", "counter"], mounts=[])
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signum=signum):
                completed = []
                previous = signal.getsignal(signum)
                def cleaner(*args, **kwargs):
                    signal.getsignal(signum)(signum, None)
                    with self.assertRaises(backends.account_tools.AccountError):
                        with backends.account_tools.account_lock(self.account):
                            self.fail("Account unlocked while cleaner was still running")
                    completed.append(True)
                with rf.lock_accounts(["oc_test"], paths=self.paths) as locks, \
                     patch.object(controller.subprocess, "run", side_effect=cleaner), \
                     patch.object(backends, "register_game_usage"), \
                     patch.object(controller.subprocess, "Popen") as popen:
                    with self.assertRaises(KeyboardInterrupt):
                        controller.start_agent("oc_test", "game0", "counter", "agent", "internal", "server",
                                               "proxy", preparation, backend=self.backend(locks=locks),
                                               agent_image="oc-image", check_cancelled=lambda: None)
                    popen.assert_not_called()
                self.assertEqual(completed, [True])
                self.assertEqual(signal.getsignal(signum), previous)

    def test_scheduler_passes_existing_account_lock_to_each_fresh_game(self):
        config = self.parse()
        images = rf.RunImages("agent-image", "server-image", "proxy-image")
        names = rf.build_container_names(["counter", "second"])
        preparation = rf.LaunchPreparation(command=["agent"], mounts=[])
        def schedule(*args, **kwargs):
            kwargs["launch_agent"]("oc_test", "game0", "counter", lambda: None)
            kwargs["launch_agent"]("oc_test", "game1", "second", lambda: None)
            return 0
        with rf.lock_accounts(["oc_test"], paths=self.paths) as locks, \
             patch.object(rf, "prepare_agent_run", return_value=preparation), \
             patch.object(rf, "run_scheduler", side_effect=schedule), \
             patch.object(controller, "start_agent") as start:
            self.assertEqual(controller.run_scheduler(config, names, {"game0": "counter", "game1": "second"},
                                                      images=images, backend=self.backend(config, locks)), 0)
            self.assertEqual(start.call_count, 2)
            for call in start.call_args_list:
                self.assertEqual(call.kwargs["backend"].account_locks["oc_test"], locks["oc_test"])
                self.assertTrue(callable(call.kwargs["check_cancelled"]))

    def test_scheduler_cancellation_prevents_cleanup_or_launch_at_each_boundary(self):
        config = self.parse()
        images = rf.RunImages("agent-image", "server-image", "proxy-image")
        names = rf.build_container_names(["counter"])
        preparation = rf.LaunchPreparation(command=["synthetic-agent"], mounts=[])
        for phase in ("prepare", "clean", "command", "log"):
            with self.subTest(phase=phase):
                def cancel_at(actual):
                    if actual == phase:
                        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                def prepare(*args, **kwargs):
                    cancel_at("prepare")
                    return preparation
                def command(*args, **kwargs):
                    cancel_at("command")
                    return ["synthetic-agent"]
                def log(message, *args):
                    if message.startswith("starting game"):
                        cancel_at("log")
                process = Mock()
                process.poll.return_value = None
                with patch.object(rf, "create_network"), patch.object(rf, "remove_network"), \
                     patch.object(rf, "prepare_game_directory"), patch.object(rf, "stop_all") as stop, \
                     patch.object(rf, "start_proxy", return_value=rf.RunningContainer("proxy", process)), \
                     patch.object(rf, "start_server", return_value=rf.RunningContainer("server", process)), \
                     patch.object(rf, "health_check_ok", return_value=True), \
                     patch.object(rf, "prepare_agent_run", side_effect=prepare), \
                     patch.object(rf, "log_info", side_effect=log), \
                     patch.object(backends, "clean_opencode_account", side_effect=lambda *a, **kw: cancel_at("clean")) as clean, \
                     patch.object(controller, "docker_agent_command", side_effect=command), \
                     patch.object(controller.subprocess, "Popen") as popen:
                    with self.assertRaises(KeyboardInterrupt):
                        controller.run_scheduler(config, names, {"game0": "counter"},
                                                 images=images, backend=self.backend(config, {"oc_test": 123}))
                    self.assertEqual(clean.call_count, 0 if phase == "prepare" else 1)
                    popen.assert_not_called()
                    stop.assert_called_once()

    def test_deferred_shutdown_is_preserved_when_cleaner_spawn_fails(self):
        previous = signal.getsignal(signal.SIGTERM)
        def fail(*args, **kwargs):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            raise OSError("synthetic cleaner spawn failure")
        with rf.lock_accounts(["oc_test"], paths=self.paths) as locks, \
             patch.object(controller.subprocess, "run", side_effect=fail):
            with self.assertRaises(KeyboardInterrupt):
                backends.clean_opencode_account("oc_test", account_lock_fd=locks["oc_test"],
                                                  agent_image="oc-image", paths=self.paths)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_controller_holds_account_locks_through_check_and_scheduler(self):
        config = self.parse()
        image = rf.RunImages("agent-image", "server-image", "proxy-image")
        @contextmanager
        def images(*args, **kwargs):
            yield image
        def assert_locked(*args, **kwargs):
            with self.assertRaises(backends.account_tools.AccountError):
                with backends.account_tools.account_lock(self.account):
                    self.fail("Controller released its account before finishing")
            if "backend" in kwargs:
                fd = kwargs["backend"].account_locks["oc_test"]
                self.assertEqual(os.fstat(fd).st_ino, (self.paths.accounts_dir / ".oc_test.opencode.lock").stat().st_ino)
            return 0
        for name in ("verify_environment_variables", "verify_container_names_available", "verify_network_names_available",
                     "prepare_run_root", "setup_runner_logging", "make_run_writable"):
            self.enterContext(patch.object(rf, name))
        with patch.object(controller, "parse_run_config", return_value=config), \
             patch.object(rf, "built_run_images", images), \
             patch.object(backends, "verify_opencode_accounts", side_effect=assert_locked) as check, \
             patch.object(controller, "run_scheduler", side_effect=assert_locked) as scheduler, \
             patch.object(backends, "ensure_account_not_in_use"):

            self.assertEqual(controller.main(["--run-root", str(self.paths.root),
                                              "--accounts-dir", str(self.paths.accounts_dir)]), 0)
            check.assert_called_once()
            scheduler.assert_called_once()
        with backends.account_tools.account_lock(self.account):
            pass

    def test_shutdown_stops_container_even_if_docker_client_has_exited(self):
        process = Mock()
        process.poll.return_value = 1
        with patch.object(rf, "container_exists", return_value=True), \
             patch.object(rf.subprocess, "run", side_effect=[Mock(returncode=1), Mock(returncode=0)]) as run:
            rf.stop_container("agent", process, "OpenCode agent")
        self.assertEqual(run.call_args_list[0].args[0], ["docker", "stop", "--timeout", "20", "agent"])
        self.assertEqual(run.call_args_list[1].args[0], ["docker", "kill", "agent"])
        process.wait.assert_called_once_with(timeout=5.0)

    def test_shutdown_failure_prevents_silent_account_reuse(self):
        process = Mock()
        process.poll.return_value = 1
        with patch.object(rf, "container_exists", return_value=True), \
             patch.object(rf.subprocess, "run", return_value=Mock(returncode=1)):
            with self.assertRaisesRegex(RuntimeError, "stop it before using its account again"):
                rf.stop_container("agent", process, "OpenCode agent")
        process.wait.assert_called_once_with(timeout=5.0)


if __name__ == "__main__":
    unittest.main()
