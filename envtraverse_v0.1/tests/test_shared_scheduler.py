from __future__ import annotations

from pathlib import Path
import signal
import subprocess
import sys
import unittest
from unittest.mock import Mock, call, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_funs as rf


class SharedSchedulerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = rf.EnvironmentConfig("example", {}, [])
        self.mapping = {"alias-first": "first", "alias-second": "second", "alias-third": "third"}
        self.names = rf.build_container_names(list(self.mapping.values()))
        self.images = rf.RunImages.create()
        self.tick = 0
        self.finish_at = {}
        self.exit_codes = {}
        self.server_exits = {}
        self.launches = []
        self.cleanup_order = []
        self.previous_handlers = {signal.SIGINT: signal.SIG_DFL, signal.SIGTERM: signal.SIG_IGN}
        self.handlers = dict(self.previous_handlers)

        def install_handler(signum, handler):
            previous = self.handlers[signum]
            self.handlers[signum] = handler
            return previous

        self.signals = self.enterContext(patch.object(rf.signal, "signal", side_effect=install_handler))
        self.sleep = self.enterContext(patch.object(rf.time, "sleep", side_effect=self.advance_clock))
        self.enterContext(patch.object(rf.time, "monotonic", side_effect=lambda: self.tick))
        self.log = self.enterContext(patch.object(rf, "log_info"))
        self.enterContext(patch.object(rf, "LOGGER"))
        self.prepare = self.enterContext(patch.object(rf, "prepare_game_directory"))
        self.create_network = self.enterContext(patch.object(rf, "create_network"))
        self.remove_network = self.enterContext(patch.object(
            rf, "remove_network", side_effect=lambda name: self.cleanup_order.append(("network", name)),
        ))
        self.stop_container = self.enterContext(patch.object(
            rf, "stop_container", side_effect=lambda name, *_args: self.cleanup_order.append(("container", name)),
        ))
        self.proxy = rf.RunningContainer(self.names.proxy, Mock(poll=Mock(return_value=None)))
        self.start_proxy = self.enterContext(patch.object(rf, "start_proxy", return_value=self.proxy))
        self.start_server = self.enterContext(patch.object(rf, "start_server", side_effect=self.launch_server))
        self.real_health_check = rf.health_check_ok
        self.health = self.enterContext(patch.object(rf, "health_check_ok", return_value=True))
        self.launch = Mock(side_effect=self.launch_agent)
        # Fail if a missed mock would invoke Docker.
        self.enterContext(patch.object(rf.subprocess, "run", side_effect=AssertionError("Unexpected subprocess")))
        self.enterContext(patch.object(rf.subprocess, "Popen", side_effect=AssertionError("Unexpected subprocess")))

    def advance_clock(self, seconds) -> None:
        self.assertEqual(seconds, 1.0)
        self.tick += 1
        if self.tick > 20:
            self.fail("Scheduler did not finish within the simulated run")

    def launch_server(self, name, internet, internal, environment, alias, game, **kwargs):
        def poll():
            at, code = self.server_exits.get(game, (99, 0))
            return code if self.tick >= at else None
        return rf.RunningContainer(name, Mock(poll=Mock(side_effect=poll)))

    def launch_agent(self, slot, alias, game):
        self.launches.append((self.tick, slot, alias, game))
        process = Mock(poll=Mock(side_effect=lambda: (
            self.exit_codes.get(game, 0) if self.tick >= self.finish_at.get(game, 0) else None
        )))
        return rf.RunningContainer(self.names.agents[game], process)

    def run_scheduler(self, slots=("worker",)):
        return rf.run_scheduler(
            self.environment, self.names, self.mapping, images=self.images, slots=slots, launch_agent=self.launch,
        )

    def assert_common_cleanup(self):
        self.assertEqual(self.cleanup_order[-3:], [
            ("container", self.names.proxy),
            ("network", self.names.internet_network),
            ("network", self.names.internal_network),
        ])
        self.assertEqual(self.handlers, self.previous_handlers)

    def assert_pair_stopped(self, game, *, agent=True):
        server = ("container", self.names.servers[game])
        self.assertEqual(self.cleanup_order.count(server), 1)
        if agent:
            agent_event = ("container", self.names.agents[game])
            self.assertEqual(self.cleanup_order.count(agent_event), 1)
            self.assertLess(self.cleanup_order.index(agent_event), self.cleanup_order.index(server))

    def test_each_game_has_its_own_server_and_shared_infrastructure_is_created_once(self):
        self.assertEqual(self.run_scheduler(), 0)
        self.assertEqual([entry[3] for entry in self.launches], ["first", "second", "third"])
        self.assertEqual(self.create_network.call_args_list, [
            call(self.names.internal_network, internal=True), call(self.names.internet_network),
        ])
        self.start_proxy.assert_called_once_with(
            self.names.proxy, self.names.internet_network, self.names.internal_network, image=self.images.proxy,
        )
        for alias, game in self.mapping.items():
            self.start_server.assert_any_call(
                self.names.servers[game], self.names.internet_network, self.names.internal_network,
                self.environment, alias, game, image=self.images.server,
            )
            self.prepare.assert_any_call(alias)
            self.assert_pair_stopped(game)
        self.assertEqual(self.start_server.call_count, 3)
        # Once ready, only process exit is checked; busy game servers are never health-probed.
        self.assertEqual(self.health.call_count, 3)
        self.assert_common_cleanup()

    def test_failed_agent_is_logged_and_pending_games_continue_without_retry(self):
        self.exit_codes["first"] = 17
        self.assertEqual(self.run_scheduler(), 1)
        self.assertEqual([entry[3] for entry in self.launches], ["first", "second", "third"])
        self.log.assert_any_call("game %s in slot %s stopped with exit code %s", "first", "worker", 17)
        self.assert_common_cleanup()

    def test_two_slots_reuse_first_free_slot_and_stop_previous_server_before_reuse(self):
        self.finish_at["first"] = 4
        original = self.start_server.side_effect

        def start(*args, **kwargs):
            if args[5] == "third":
                self.assert_pair_stopped("second")
                self.assertNotIn(("container", self.names.servers["first"]), self.cleanup_order)
            return original(*args, **kwargs)

        self.start_server.side_effect = start
        self.assertEqual(self.run_scheduler(("one", "two")), 0)
        self.assertEqual(self.launches, [
            (0, "one", "alias-first", "first"),
            (0, "two", "alias-second", "second"),
            (2, "two", "alias-third", "third"),
        ])
        self.assert_common_cleanup()

    def test_slow_server_startup_does_not_block_other_pairs(self):
        self.health.side_effect = lambda server: server.container_name != self.names.servers["first"] or self.tick >= 3
        self.assertEqual(self.run_scheduler(("one", "two")), 0)
        self.assertEqual(self.launches, [
            (0, "two", "alias-second", "second"),
            (2, "two", "alias-third", "third"),
            (3, "one", "alias-first", "first"),
        ])

    def test_one_slot_per_game_launches_all_agents_on_first_tick(self):
        self.assertEqual(self.run_scheduler(tuple(self.mapping)), 0)
        self.assertEqual([entry[0] for entry in self.launches], [0, 0, 0])
        self.assert_common_cleanup()

    def test_server_exit_only_stops_its_pair_and_pending_games_continue(self):
        self.finish_at["first"] = 99
        self.finish_at["second"] = 5
        self.server_exits["first"] = (1, 23)
        self.assertEqual(self.run_scheduler(("one", "two")), 1)
        self.assertEqual(self.launches[-1], (2, "one", "alias-third", "third"))
        for game in self.mapping.values():
            self.assert_pair_stopped(game)
        self.log.assert_any_call("server for game %s stopped unexpectedly with exit code %s", "first", 23)
        self.assert_common_cleanup()

    def test_server_exit_before_ready_is_failure_even_with_exit_code_zero(self):
        self.server_exits["first"] = (0, 0)
        self.assertEqual(self.run_scheduler(), 1)
        self.assertEqual([entry[3] for entry in self.launches], ["second", "third"])
        self.assert_pair_stopped("first", agent=False)

    def test_server_start_timeout_releases_slot(self):
        self.health.side_effect = lambda server: server.container_name != self.names.servers["first"]
        with patch.object(rf, "SERVER_START_TIMEOUT_SECONDS", 2):
            self.assertEqual(self.run_scheduler(), 1)
        self.assertEqual(self.launches[0], (3, "worker", "alias-second", "second"))
        self.assert_pair_stopped("first", agent=False)
        self.log.assert_any_call("server startup timed out for game %s", "first")

    def test_proxy_exit_stops_all_active_pairs_without_starting_pending_game(self):
        self.finish_at.update(first=99, second=99)
        self.proxy.process.poll.side_effect = [None, 31]
        self.assertEqual(self.run_scheduler(("one", "two")), 31)
        self.assertEqual(self.launch.call_count, 2)
        self.assert_pair_stopped("first")
        self.assert_pair_stopped("second")
        self.assert_common_cleanup()

    def test_signal_during_health_stops_startup(self):
        def health(server):
            self.handlers[signal.SIGTERM](signal.SIGTERM, None)
            return True
        self.health.side_effect = health
        self.assertEqual(self.run_scheduler(), 130)
        self.launch.assert_not_called()
        self.assert_pair_stopped("first", agent=False)
        self.assert_common_cleanup()

    def test_signal_during_launch_cleans_up_agent_and_both_started_servers(self):
        def launch(slot, alias, game):
            agent = self.launch_agent(slot, alias, game)
            self.handlers[signal.SIGINT](signal.SIGINT, None)
            return agent
        self.launch.side_effect = launch
        self.assertEqual(self.run_scheduler(("one", "two")), 130)
        self.assertEqual(self.launch.call_count, 1)
        self.assert_pair_stopped("first")
        self.assert_pair_stopped("second", agent=False)
        self.sleep.assert_not_called()
        self.assert_common_cleanup()

    def test_failed_second_network_only_cleans_up_first_network(self):
        self.create_network.side_effect = [None, RuntimeError("Network creation failed")]
        with self.assertRaisesRegex(RuntimeError, "Network creation failed"):
            self.run_scheduler()
        self.assertEqual(self.cleanup_order, [("network", self.names.internal_network)])
        self.assertEqual(self.handlers, self.previous_handlers)
        self.start_proxy.assert_not_called()

    def test_proxy_start_failure_removes_networks(self):
        self.start_proxy.side_effect = RuntimeError("Proxy startup failed")
        with self.assertRaisesRegex(RuntimeError, "Proxy startup failed"):
            self.run_scheduler()
        self.assertEqual(self.cleanup_order, [
            ("network", self.names.internet_network), ("network", self.names.internal_network),
        ])
        self.assertEqual(self.handlers, self.previous_handlers)
        self.start_server.assert_not_called()

    def test_server_launch_exception_skips_only_that_game(self):
        original = self.start_server.side_effect
        def start(*args, **kwargs):
            if args[5] == "first":
                raise RuntimeError("Server launch failed")
            return original(*args, **kwargs)
        self.start_server.side_effect = start
        self.assertEqual(self.run_scheduler(), 1)
        self.assertEqual([entry[3] for entry in self.launches], ["second", "third"])
        self.assert_common_cleanup()

    def test_agent_launch_exception_stops_its_server_and_continues(self):
        def launch(slot, alias, game):
            if game == "first":
                raise RuntimeError("Agent launch failed")
            return self.launch_agent(slot, alias, game)
        self.launch.side_effect = launch
        self.assertEqual(self.run_scheduler(("one", "two")), 1)
        self.assertEqual([entry[3] for entry in self.launches], ["second", "third"])
        self.assert_pair_stopped("first", agent=False)
        self.assert_common_cleanup()

    def test_cleanup_exception_does_not_skip_other_pairs_proxy_networks_or_handlers(self):
        self.finish_at.update(first=99, second=99)
        self.proxy.process.poll.side_effect = [None, 31]
        original = self.stop_container.side_effect
        def stop(name, *args):
            original(name, *args)
            if name == self.names.agents["first"]:
                raise RuntimeError("Stop failed")
        self.stop_container.side_effect = stop
        with self.assertRaisesRegex(RuntimeError, "Stop failed"):
            self.run_scheduler(("one", "two"))
        self.assert_pair_stopped("first")
        self.assert_pair_stopped("second")
        self.assert_common_cleanup()

    def test_empty_or_duplicate_slots_fail_before_creating_resources(self):
        for slots in ((), ("same", "same")):
            with self.assertRaisesRegex(ValueError, "Worker slots"):
                self.run_scheduler(slots)
        self.signals.assert_not_called()
        self.create_network.assert_not_called()

    def test_health_probe_has_a_bounded_subprocess_timeout(self):
        with patch.object(rf.subprocess, "run", side_effect=subprocess.TimeoutExpired("docker", 2)) as probe:
            self.assertFalse(self.real_health_check(self.proxy))
        self.assertEqual(probe.call_args.kwargs["timeout"], 2.0)


class ContainerStopTest(unittest.TestCase):
    def test_shutdown_waits_for_a_container_still_being_created(self):
        process = Mock(poll=Mock(return_value=None))
        events = []
        def exists(_name):
            events.append("inspect")
            return len(events) >= 2
        def run(command, **kwargs):
            events.append(command[1])
            return Mock(returncode=0)
        with (
            patch.object(rf, "container_exists", side_effect=exists),
            patch.object(rf.time, "sleep") as sleep,
            patch.object(rf.subprocess, "run", side_effect=run),
            patch.object(rf, "log_info"),
        ):
            rf.stop_container("starting-container", process, "server")
        self.assertEqual(events, ["inspect", "inspect", "stop"])
        sleep.assert_called_once_with(0.1)
        process.wait.assert_called_once_with(timeout=5.0)

    def test_failed_launch_does_not_wait_for_a_nonexistent_container(self):
        process = Mock(poll=Mock(return_value=125))
        with patch.object(rf, "container_exists") as exists, patch.object(rf.subprocess, "run") as run:
            rf.stop_container("failed-container", process, "server")
        exists.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
