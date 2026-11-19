from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import run_controller as controller
from src.agent_api import AgentMount, LaunchPreparation
from src.controller import mem_volumes as mv
from src.controller import run_funs as rf
from tests.test_run_launchers import IMAGES, make_backend, make_config
from src.controller import run_backends as rb


class MemVolumeConfigurationTest(unittest.TestCase):
    def parse(self, entries, *, existing=()):
        return mv.parse_mem_disk_mounts({"mem_disk_mounts": entries}, Path("run.yaml"), existing=existing)

    def test_sizes_and_independent_mounts(self):
        mounts = self.parse([{"mount": "/latest_frame/", "size": "10mb"},
                             {"mount": "/scratch", "size": "1.5GiB"}])
        self.assertEqual(mounts, (mv.MemDiskMount("/latest_frame", 10 * 1024**2),
                                  mv.MemDiskMount("/scratch", 3 * 1024**3 // 2)))
        self.assertEqual(self.parse([{"mount": "/ram", "size": 500}])[0].size_bytes, 500)
        self.assertEqual(mv.parse_mem_disk_mounts({}, Path("run.yaml")), ())

    def test_invalid_entries_and_sizes(self):
        for value in (None, {}, "10mb", [{"mount": "/ram"}], [{"mount": "/ram", "size": "10mb", "extra": True}]):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, "mem_disk_mounts"):
                self.parse(value)
        for size in (None, True, False, 0, -1, "0mb", "-10mb", "nan", "inf", "1e20", "2pb", "0.1b", 2**63):
            with self.subTest(size=size), self.assertRaisesRegex(RuntimeError, "mem_disk_mounts"):
                self.parse([{"mount": "/ram", "size": size}])

    def test_mounts_cannot_hide_existing_mounts_or_overlap_each_other(self):
        for target in ("ram", "/", "//", "/ram/../data", "/ram\n", "/ram,readonly", "/ram:other",
                       "/data", "/data/child", "/client", "/observations", "/home/user",
                       "/home/user/run/agent_run", "/home/user/.codex", "/dev/shm", "/app"):
            with self.subTest(target=target), self.assertRaisesRegex(RuntimeError, "mem_disk_mounts"):
                self.parse([{"mount": target, "size": "10mb"}])
        for first, second in (("/ram", "/ram"), ("/ram", "/ram/child"), ("/ram/child", "/ram"),
                              ("/ram/./", "//ram")):
            with self.subTest(first=first, second=second), self.assertRaisesRegex(RuntimeError, "overlaps"):
                self.parse([{"mount": first, "size": "10mb"}, {"mount": second, "size": "10mb"}])
        with self.assertRaisesRegex(RuntimeError, "overlaps"):
            self.parse([{"mount": "/external", "size": "10mb"}], existing=["/external/socket"])

    def test_run_configuration_reads_mounts_and_reserves_resource_variable(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = rf.RunPaths(Path(temporary))
            (paths.accounts_dir / "codex01").mkdir(parents=True)
            config = paths.root / "config.yaml"
            config.write_text("agent: agent_v1\nenvironment: example\ngames: [counter]\naccounts: [codex01]\n"
                              "mem_disk_mounts:\n  - mount: /latest_frame\n    size: 10mb\n")
            parsed = controller.parse_run_config(config, paths=paths)
            self.assertEqual(parsed.mem_disk_mounts, (mv.MemDiskMount("/latest_frame", 10 * 1024**2),))
            self.assertNotIn("mem_disk_mounts", parsed.environment.parameters)
            with self.assertRaisesRegex(RuntimeError, "managed"):
                rf.parse_environment_config({"environment": "example", "environment_variables": ["ENVTRAVERSE_RESOURCES"]}, config)


class MemVolumeLaunchTest(unittest.TestCase):
    def test_agent_and_server_share_volume_without_exposing_context_to_agent(self):
        mount = mv.MemDiskMount("/latest_frame", 10 * 1024**2)
        resources = mv.resource_context([mount], "a" * 32)
        other = mv.resource_context([mount], "b" * 32)
        self.assertNotEqual(resources["mem_disk_mounts"][0]["volume"], other["mem_disk_mounts"][0]["volume"])
        environment = rf.EnvironmentConfig("example", {"target": 3}, [])
        server = rf.docker_server_command("server", "internet", "internal", environment, "a" * 32, "counter",
                                          image=IMAGES.server, resources=resources)
        agent = controller.docker_agent_command("codex01", "agent", "internal", "server", "proxy",
            LaunchPreparation(["true"]), agent_image=IMAGES.agent,
            backend=make_backend(make_config(rb.CODEX_CHATGPT)), resources=resources)
        argument = mv.volume_mount_args(resources)[1]
        self.assertIn(argument, server)
        self.assertIn(argument, agent)
        self.assertLess(server.index(argument), server.index(IMAGES.server))
        self.assertLess(agent.index(argument), agent.index(IMAGES.agent))
        encoded = next(value for value in server if value.startswith("ENVTRAVERSE_RESOURCES="))
        self.assertEqual(json.loads(encoded.split("=", 1)[1]), resources)
        self.assertFalse(any(value.startswith("ENVTRAVERSE_RESOURCES") for value in agent))
        self.assertEqual(json.loads(server[server.index("--parameters") + 1]), {"target": 3})

    def test_agent_preparation_mount_collisions_are_rejected_before_launch(self):
        config = replace(make_config(rb.CODEX_CHATGPT), mem_disk_mounts=(mv.MemDiskMount("/custom", 1024),))
        names = rf.build_container_names(config.games)
        with patch.object(rf, "run_scheduler", return_value=0) as scheduler, \
                patch.object(controller, "start_agent") as start, \
                patch.object(rf, "prepare_agent_run", return_value=LaunchPreparation(
                    ["true"], [AgentMount(Path("/tmp"), "/custom/child")])):
            controller.run_scheduler(config, names, {"a" * 32: "counter"}, images=IMAGES, backend=make_backend(config))
            with self.assertRaisesRegex(RuntimeError, "overlaps"):
                scheduler.call_args.kwargs["launch_agent"]("codex01", "a" * 32, "counter", lambda: None)
            start.assert_not_called()

    def test_selected_agent_receives_same_resource_context(self):
        config = replace(make_config(rb.CODEX_CHATGPT), mem_disk_mounts=(mv.MemDiskMount("/ram", 1024),))
        names = rf.build_container_names(config.games)
        with patch.object(rf, "run_scheduler", return_value=0) as scheduler, \
                patch.object(controller, "start_agent") as start, \
                patch.object(rf, "prepare_agent_run", return_value=LaunchPreparation(["true"])):
            controller.run_scheduler(config, names, {"a" * 32: "counter"}, images=IMAGES, backend=make_backend(config))
            scheduler.call_args.kwargs["launch_agent"]("codex01", "a" * 32, "counter", lambda: None)
            self.assertEqual(start.call_args.kwargs["resources"], mv.resource_context(config.mem_disk_mounts, "a" * 32))
            self.assertEqual(scheduler.call_args.kwargs["mem_disk_mounts"], config.mem_disk_mounts)


class MemVolumeLifecycleTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.log = Mock()
        self.manager = mv.MemVolumeManager([mv.MemDiskMount("/ram", 1024)], self.log)
        self.alias = "a" * 32
        self.name = "envtraverse-mem-" + self.alias + "-0"
        self.run = self.enterContext(patch.object(mv.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")))

    def create(self):
        return self.manager.create_game(self.alias, self.directory)

    def test_create_records_names_and_configures_tmpfs_before_launch(self):
        context = self.create()
        self.assertEqual(json.loads((self.directory / "resources.json").read_text()), context)
        command = self.run.call_args.args[0]
        self.assertIn("type=tmpfs", command)
        self.assertIn("device=tmpfs", command)
        self.assertIn(f"o=size=1024,uid={os.getuid()},gid={os.getgid()},mode=1777", command)
        self.assertEqual(command[-1], self.name)
        self.manager.release_game(self.alias)
        self.assertFalse(self.manager.pending)
        self.manager.close()
        self.assertEqual(self.run.call_count, 2)

    def test_external_holder_defers_removal_until_final_pass(self):
        self.create()
        self.run.side_effect = [subprocess.CompletedProcess([], 1, "", "volume is in use"),
                               subprocess.CompletedProcess([], 1, "", "volume is in use"),
                               subprocess.CompletedProcess([], 0, "", "")]
        self.manager.release_game(self.alias)
        self.assertIn(self.name, self.manager.pending)
        with patch.object(mv.time, "sleep") as sleep:
            self.manager.close()
        sleep.assert_called_once_with(1.0)
        self.assertFalse(self.manager.pending)
        self.assertTrue(any("unremoved" in call.args[0] for call in self.log.call_args_list))

    def test_missing_volume_is_success_but_daemon_failure_is_retained(self):
        self.create()
        self.run.return_value = subprocess.CompletedProcess([], 1, "", "Cannot connect to Docker daemon")
        self.manager.release_game(self.alias)
        self.assertIn(self.name, self.manager.pending)
        self.run.return_value = subprocess.CompletedProcess([], 1, "", "Error response from daemon: no such volume")
        self.manager.close()
        self.assertFalse(self.manager.pending)

    def test_partial_creation_is_tracked_and_removable(self):
        self.manager = mv.MemVolumeManager([mv.MemDiskMount("/ram", 1024), mv.MemDiskMount("/scratch", 1024)], self.log)
        self.run.side_effect = [subprocess.CompletedProcess([], 0, "", ""), subprocess.TimeoutExpired("docker", 30)]
        with self.assertRaises(subprocess.TimeoutExpired):
            self.create()
        self.assertEqual(len(self.manager.pending), 2)
        self.assertEqual(len(json.loads((self.directory / "resources.json").read_text())["mem_disk_mounts"]), 2)
        self.run.side_effect = None
        self.manager.close()
        self.assertFalse(self.manager.pending)

    def test_cleanup_has_one_global_deadline_and_reports_leftovers(self):
        self.create()
        other = "envtraverse-mem-" + "b" * 32 + "-0"
        self.manager.pending[other] = "owned"
        clock = [0.0]
        def run(*args, **kwargs):
            clock[0] += kwargs["timeout"]
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
        self.run.side_effect = run
        with patch.object(mv, "CLEANUP_TIMEOUT_SECONDS", 2.0), \
                patch.object(mv.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(mv.time, "sleep") as sleep, \
                self.assertRaisesRegex(RuntimeError, self.name):
            self.manager.close()
        self.assertEqual(clock[0], 2.0)
        self.assertEqual(self.run.call_args.kwargs["timeout"], 2.0)
        sleep.assert_not_called()
        self.assertEqual(len(self.manager.pending), 2)
        self.log.assert_any_call("RAM volume cleanup failed: %s: %s", other, "owned")
