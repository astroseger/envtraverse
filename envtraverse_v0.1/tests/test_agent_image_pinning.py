from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_controller as accounts_controller
import run_controller_with_api_key as api_controller
import run_funs as rf


class RunImagesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(rf, "RUN_DIR", self.directory))
        self.enterContext(patch.object(rf, "log_info"))
        self.logger = self.enterContext(patch.object(rf, "LOGGER"))
        self.exists = self.enterContext(patch.object(rf, "image_exists", return_value=False))
        self.build = self.enterContext(patch.object(rf, "build_dockers"))
        self.remove = self.enterContext(patch.object(rf, "remove_run_images"))
        # A missing mock must never run real Docker from these tests.
        self.enterContext(patch.object(rf.subprocess, "run", side_effect=AssertionError("Unexpected subprocess")))

    def test_unique_names_share_one_uuid_and_have_no_version_dependency(self) -> None:
        first, second = rf.RunImages.create(), rf.RunImages.create()
        self.assertEqual(len(set(asdict(first).values()) | set(asdict(second).values())), 6)
        suffixes = [tag.split(":")[1] for tag in asdict(first).values()]
        self.assertEqual(len(set(suffixes)), 1)
        self.assertRegex(suffixes[0], r"^run-[0-9a-f]{32}$")

    def test_manifest_is_saved_before_build_and_cleanup_waits_for_body(self) -> None:
        def build(environment, agent, images):
            self.assertEqual(agent, "agent_v1")
            self.assertEqual(environment, "example")
            self.assertEqual(json.loads((self.directory / "docker_images.json").read_text()), asdict(images))

        self.build.side_effect = build
        with rf.built_run_images("example", "agent_v1") as images:
            self.remove.assert_not_called()
            self.build.assert_called_once_with("example", "agent_v1", images)
        self.remove.assert_called_once_with(images)

    def test_partial_build_failure_still_removes_the_run_images(self) -> None:
        self.build.side_effect = RuntimeError("Second image failed")
        with self.assertRaisesRegex(RuntimeError, "Second image failed"):
            with rf.built_run_images("example", "agent_v1"):
                self.fail("Failed build yielded images")
        images = self.build.call_args.args[2]
        self.remove.assert_called_once_with(images)

    def test_preflight_or_scheduler_failure_also_removes_images(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Preflight failed"):
            with rf.built_run_images("example", "agent_v1") as images:
                raise RuntimeError("Preflight failed")
        self.remove.assert_called_once_with(images)

    def test_existing_image_is_neither_built_nor_removed(self) -> None:
        self.exists.side_effect = [False, True]
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            with rf.built_run_images("example", "agent_v1"):
                self.fail("Existing tag was accepted")
        self.build.assert_not_called()
        self.remove.assert_not_called()

    def test_existing_manifest_is_preserved_without_building_or_cleanup(self) -> None:
        manifest = self.directory / "docker_images.json"
        manifest.write_text("existing", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            with rf.built_run_images("example", "agent_v1"):
                self.fail("Existing manifest was overwritten")
        self.assertEqual(manifest.read_text(), "existing")
        self.build.assert_not_called()
        self.remove.assert_not_called()

    def test_startup_signal_cleans_up_and_restores_handlers(self) -> None:
        previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}

        def build(*_args):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        self.build.side_effect = build
        with self.assertRaises(KeyboardInterrupt):
            with rf.built_run_images("example", "agent_v1"):
                self.fail("Interrupted build yielded images")
        self.remove.assert_called_once()
        self.assertEqual({number: signal.getsignal(number) for number in previous}, previous)

    def test_signals_during_image_cleanup_cannot_skip_remaining_tags(self) -> None:
        previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}

        def remove(_images):
            for number in previous:
                self.assertEqual(signal.getsignal(number), signal.SIG_IGN)

        self.remove.side_effect = remove
        with rf.built_run_images("example", "agent_v1"):
            pass
        self.remove.assert_called_once()
        self.assertEqual({number: signal.getsignal(number) for number in previous}, previous)

    def test_both_controllers_keep_images_alive_for_account_checks_and_scheduler(self) -> None:
        for controller in (accounts_controller, api_controller):
            with self.subTest(controller=controller.__name__), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                stack.enter_context(patch.object(rf, "RUN_DIR", Path(directory)))
                config = dict(
                    games=["counter"], environment=rf.EnvironmentConfig("example", {}, []),
                    agent="agent_v1", parsed_run_config={"model": "test-model", "reasoning_effort": "medium"},
                )
                if controller is accounts_controller:
                    config["accounts"] = ["codex01"]
                config = controller.RunConfig(**config)
                stack.enter_context(patch.object(controller, "parse_run_config", return_value=config))
                stack.enter_context(patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}))
                for name in ("verify_container_names_available", "verify_network_names_available", "prepare_run_root", "setup_runner_logging", "make_run_writable"):
                    stack.enter_context(patch.object(rf, name))
                if controller is api_controller:
                    stack.enter_context(patch.object(controller, "confirm_api_key_run"))
                preflight = "verify_codex_accounts" if controller is accounts_controller else "verify_codex_api_key"
                self.remove.reset_mock()
                stack.enter_context(patch.object(controller, preflight, side_effect=lambda *_args: self.remove.assert_not_called()))

                def schedule(*_args, images):
                    self.remove.assert_not_called()
                    self.assertEqual(self.build.call_args.args[2], images)
                    return 0

                stack.enter_context(patch.object(controller, "run_scheduler", side_effect=schedule))
                self.assertEqual(controller.main([]), 0)
                self.remove.assert_called_once_with(self.build.call_args.args[2])


class ImageRemovalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.images = rf.RunImages.create()
        self.enterContext(patch.object(rf, "log_info"))
        self.logger = self.enterContext(patch.object(rf, "LOGGER"))

    def test_only_owned_tags_are_removed_without_force_or_global_pruning(self) -> None:
        with patch.object(rf, "image_exists", return_value=True), patch.object(rf.subprocess, "run", return_value=Mock(returncode=0)) as run:
            rf.remove_run_images(self.images)
        self.assertEqual(run.call_args_list, [
            call(["docker", "image", "rm", "--no-prune", tag], cwd=ROOT, check=False)
            for tag in asdict(self.images).values()
        ])

    def test_missing_images_from_partial_build_are_skipped(self) -> None:
        with patch.object(rf, "image_exists", side_effect=[True, False, False]), patch.object(rf.subprocess, "run", return_value=Mock(returncode=0)) as run:
            rf.remove_run_images(self.images)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][-1], self.images.agent)

    def test_cleanup_failure_does_not_skip_other_images_or_raise(self) -> None:
        with patch.object(rf, "image_exists", return_value=True), patch.object(rf.subprocess, "run", side_effect=[Mock(returncode=1), OSError("Docker unavailable"), Mock(returncode=0)]) as run:
            rf.remove_run_images(self.images)
        self.assertEqual(run.call_count, 3)
        self.logger.warning.assert_called_once()
        self.logger.exception.assert_called_once()

    def test_daemon_failure_is_not_treated_as_a_missing_image(self) -> None:
        with patch.object(rf.subprocess, "run", return_value=Mock(returncode=1, stderr="Cannot connect")):
            with self.assertRaisesRegex(RuntimeError, "Cannot connect"):
                rf.image_exists(self.images.agent)


class BuildInterruptionTest(unittest.TestCase):
    def test_missing_or_invalid_built_image_fails_without_falling_back_to_other_tags(self) -> None:
        for result in (Mock(returncode=1, stdout=""), Mock(returncode=0, stdout=""), Mock(returncode=0, stdout="not-an-id")):
            with self.subTest(result=result), patch.object(rf.subprocess, "Popen", return_value=Mock(wait=Mock(return_value=0))), patch.object(rf.subprocess, "run", return_value=result) as inspect, patch.object(rf, "log_info"):
                images = rf.RunImages.create()
                with self.assertRaisesRegex(RuntimeError, "could not inspect built agent image"):
                    rf.build_dockers("example", "agent_v1", images)
                inspect.assert_called_once_with(
                    ["docker", "image", "inspect", "--format", "{{.Id}}", images.agent],
                    cwd=ROOT, capture_output=True, text=True, check=False,
                )

    def test_interrupted_build_terminates_and_waits_for_its_process_group(self) -> None:
        process = Mock(pid=12345, wait=Mock(side_effect=[KeyboardInterrupt, -15]))
        with (
            patch.object(rf.subprocess, "Popen", return_value=process) as popen,
            patch.object(rf.os, "killpg") as killpg,
            patch.object(rf, "log_info"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                rf.build_dockers("example", "agent_v1", rf.RunImages.create())
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        killpg.assert_called_once_with(12345, signal.SIGTERM)
        self.assertEqual(process.wait.call_args_list, [call(), call(timeout=10)])

    def test_unresponsive_interrupted_builder_is_killed(self) -> None:
        process = Mock(pid=12345, wait=Mock(side_effect=[KeyboardInterrupt, subprocess.TimeoutExpired("build", 10), -9]))
        with patch.object(rf.subprocess, "Popen", return_value=process), patch.object(rf.os, "killpg") as killpg, patch.object(rf, "log_info"):
            with self.assertRaises(KeyboardInterrupt):
                rf.build_dockers("example", "agent_v1", rf.RunImages.create())
        self.assertEqual(killpg.call_args_list, [call(12345, signal.SIGTERM), call(12345, signal.SIGKILL)])

    def test_failed_build_does_not_start_image_inspection(self) -> None:
        with patch.object(rf.subprocess, "Popen", return_value=Mock(wait=Mock(return_value=17))), patch.object(rf.subprocess, "run") as run, patch.object(rf, "log_info"):
            with self.assertRaisesRegex(RuntimeError, "exit code 17"):
                rf.build_dockers("example", "agent_v1", rf.RunImages.create())
        run.assert_not_called()


class QueuedImagePinningTest(unittest.TestCase):
    def test_queued_games_use_their_run_image_when_another_run_builds(self) -> None:
        for controller in (accounts_controller, api_controller):
            with self.subTest(controller=controller.__name__), ExitStack() as stack:
                config = dict(
                    games=["first", "second", "third"], environment=rf.EnvironmentConfig("example", {}, []),
                    agent="agent_v1", parsed_run_config={"model": "test-model", "reasoning_effort": "medium"},
                )
                if controller is accounts_controller:
                    config["accounts"] = ["codex01"]
                config = controller.RunConfig(**config)
                first, second = rf.RunImages.create(), rf.RunImages.create()
                image_store = {first.agent: "first-build"}
                launched = []

                def launch(command, **_kwargs):
                    image = command[command.index("python3") - 1]
                    launched.append(image_store[image])
                    image_store[second.agent] = "second-build"
                    self.assertIn("--pull=never", command)
                    return Mock(poll=Mock(return_value=0))

                stack.enter_context(patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}))
                for name in ("create_network", "remove_network", "stop_all", "stop_container", "log_info"):
                    stack.enter_context(patch.object(rf, name))
                stack.enter_context(patch.object(rf.time, "sleep"))
                stack.enter_context(patch.object(rf, "prepare_game_directory"))
                stack.enter_context(patch.object(rf, "prepare_agent_run", return_value=rf.LaunchPreparation(["python3", "agent_runner.py"])))
                if controller is accounts_controller:
                    stack.enter_context(patch.object(controller, "clean_codex_account"))
                stack.enter_context(patch.object(rf, "health_check_ok", return_value=True))
                for name in ("start_proxy", "start_server"):
                    stack.enter_context(patch.object(rf, name, return_value=Mock(process=Mock(poll=Mock(return_value=None)))))
                stack.enter_context(patch.object(controller.subprocess, "Popen", side_effect=launch))
                names = rf.build_container_names(config.games)
                mapping = {f"alias-{game}": game for game in config.games}
                self.assertEqual(controller.run_scheduler(config, names, mapping, images=first), 0)
                self.assertEqual(launched, ["first-build"] * 3)


if __name__ == "__main__":
    unittest.main()
