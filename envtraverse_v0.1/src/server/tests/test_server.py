from __future__ import annotations

import base64
import io
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server
from artifacts import serialize_observation
from environment_api import (
    EnvironmentError, EnvironmentStartError, FatalEnvironmentError,
    InvalidActionError, Observation, StartResult,
)
from environments.example.environment_manager import EnvironmentManager


class ServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = EnvironmentManager()
        self.game = server.GameServer(self.manager, game_id="counter", game_alias="public-alias")
        self.client = server.create_app(self.game).test_client()
        self.addCleanup(self.game.stop)

    def start(self):
        response = self.client.post("/game/start", json={"game_id": "public-alias"})
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.headers = {"X-Session-Token": payload["session"]["token"]}
        return payload

    def move(self, action):
        return self.client.post("/game/action", json={"action": action}, headers=self.headers)

    def test_only_configured_alias_can_start_and_start_is_single_use(self) -> None:
        for alias in ("counter", "other", "", None):
            response = self.client.post("/game/start", json={"game_id": alias})
            self.assertEqual(response.status_code, 400)
            self.assertFalse(self.game.start_attempted)
        started = self.start()
        self.assertEqual(set(started), {"session", "environment_description", "step"})
        self.assertEqual(set(started["session"]), {"token", "terminal"})
        repeated = self.client.post("/game/start", json={"game_id": "public-alias"})
        self.assertEqual(repeated.status_code, 400)
        self.assertEqual(self.manager.value, 0)

    def test_description_is_cached_and_requires_header_token(self) -> None:
        started = self.start()
        self.manager.target = 5
        response = self.client.get("/game/environment-description", headers=self.headers)
        self.assertEqual(response.get_json(), {"environment_description": started["environment_description"]})
        self.assertIn("Reach 3", response.get_json()["environment_description"])
        for headers in ({}, {"X-Session-Token": "unknown"}):
            self.assertEqual(self.client.get("/game/last-step", headers=headers).status_code, 400)
        token = started["session"]["token"]
        self.assertEqual(self.client.get("/game/last-step", query_string={"token": token}).status_code, 400)
        self.assertEqual(self.client.post("/game/action", json={"token": token, "action": {"id": "INC"}}).status_code, 400)

    def test_invalid_requests_and_actions_do_not_advance(self) -> None:
        self.start()
        for action in (None, [], {}, {"id": "UNKNOWN"}):
            self.assertEqual(self.move(action).status_code, 400)
            self.assertEqual(self.game.step_index, 0)
            self.assertEqual(self.manager.value, 0)
        for body in (None, [], "action"):
            response = self.client.post("/game/action", json=body, headers=self.headers)
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.move({"id": "INC"}).get_json()["step"]["step_index"], 1)

    def test_terminal_observation_and_description_remain_available(self) -> None:
        started = self.start()
        for _ in range(3):
            response = self.move({"id": "INC"})
            self.assertEqual(response.status_code, 200)
        final = response.get_json()
        self.assertTrue(final["session"]["terminal"])
        with patch.object(self.manager, "move") as move:
            self.assertEqual(self.move({"id": "INC"}).status_code, 400)
            move.assert_not_called()
        self.assertEqual(self.client.get("/game/last-step", headers=self.headers).get_json(), final)
        self.assertEqual(
            self.client.get("/game/environment-description", headers=self.headers).get_json(),
            {"environment_description": started["environment_description"]},
        )
        self.assertFalse(self.game.stopped)

    def test_response_cache_is_a_snapshot(self) -> None:
        self.start()
        payload = self.game.move({"id": "INC"})
        payload["step"]["artifacts"].clear()
        payload["action"]["id"] = "changed"
        saved = self.client.get("/game/last-step", headers=self.headers).get_json()
        self.assertEqual(saved["action"], {"id": "INC"})
        self.assertIn(b"value: 1", base64.b64decode(saved["step"]["artifacts"][0]["content_base64"]))

    def test_apps_own_independent_games(self) -> None:
        other_manager = EnvironmentManager({"target": 5})
        other_game = server.GameServer(other_manager, game_id="counter", game_alias="other")
        self.addCleanup(other_game.stop)
        other = server.create_app(other_game).test_client()
        self.start()
        started = other.post("/game/start", json={"game_id": "other"}).get_json()
        self.move({"id": "INC"})
        self.assertEqual(other_manager.value, 0)
        self.assertEqual(other_game.step_index, 0)
        self.assertNotEqual(self.game.token, started["session"]["token"])
        self.assertEqual(other.get("/game/last-step", headers=self.headers).status_code, 400)
        self.game.stop()
        response = other.post("/game/action", json={"action": {"id": "INC"}},
                              headers={"X-Session-Token": started["session"]["token"]})
        self.assertEqual(response.status_code, 200)

    def test_invalid_start_results_stop_manager_and_cannot_be_retried(self) -> None:
        results = [
            None, StartResult("", Observation({})), StartResult(None, Observation({})),
            StartResult("Rules", None), StartResult("Rules", Observation({"../escape.txt": "bad"})),
            StartResult("Rules", Observation({"state.txt": 123})),
            StartResult("Rules", Observation({}, terminal="false")),
        ]
        for result in results:
            with self.subTest(result=result):
                manager = Mock(start_environment=Mock(return_value=result))
                game = server.GameServer(manager, game_id="private-id", game_alias="alias")
                client = server.create_app(game).test_client()
                with self.assertLogs(server.LOGGER, level="ERROR"):
                    response = client.post("/game/start", json={"game_id": "alias"})
                self.assertEqual(response.status_code, 500)
                self.assertEqual(response.get_json(), {"error": "Environment request failed"})
                self.assertIsNone(game.token)
                self.assertTrue(game.failed)
                manager.stop.assert_called_once()
                self.assertEqual(client.post("/game/start", json={"game_id": "alias"}).status_code, 503)
                game.stop()
                manager.stop.assert_called_once()

    def test_start_failure_and_cleanup_failure_preserve_original_error(self) -> None:
        with patch.object(self.manager, "start_environment", side_effect=EnvironmentStartError("Unavailable")), patch.object(
            self.manager, "stop", side_effect=RuntimeError("Cleanup failed")
        ) as stop:
            with self.assertLogs(server.LOGGER, level="WARNING") as captured:
                response = self.client.post("/game/start", json={"game_id": "public-alias"})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json(), {"error": "Unavailable"})
            self.assertTrue(self.game.failed)
            stop.assert_called_once()
            self.assertIn("Cleanup failed", "\n".join(captured.output))

    def test_rejected_and_operational_errors_do_not_advance(self) -> None:
        self.start()
        for error, status in ((InvalidActionError("Bad action"), 400), (EnvironmentError("Unavailable"), 503)):
            with self.subTest(error=error), patch.object(self.manager, "move", side_effect=error):
                with self.assertLogs(server.LOGGER, level="WARNING"):
                    response = self.move({"id": "INC"})
                self.assertEqual(response.status_code, status)
                self.assertEqual(self.game.step_index, 0)
                self.assertFalse(self.game.failed)

    def test_fatal_error_marks_only_this_server_for_shutdown(self) -> None:
        self.start()
        with patch.object(self.manager, "move", side_effect=FatalEnvironmentError("Backend closed")):
            with self.assertLogs(server.LOGGER, level="ERROR"):
                response = self.move({"id": "INC"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"error": "Backend closed"})
        self.assertTrue(self.game.failed)
        self.assertEqual(self.client.get("/health").status_code, 503)
        self.assertEqual(self.game.step_index, 0)

    def test_unexpected_errors_are_logged_without_exposing_details(self) -> None:
        self.start()
        with patch.object(self.manager, "move", side_effect=RuntimeError("ARC3 secret-game-version")):
            with self.assertLogs(server.LOGGER, level="ERROR") as captured:
                response = self.move({"id": "INC"})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json(), {"error": "Environment request failed"})
        self.assertIn("ARC3 secret-game-version", "\n".join(captured.output))
        self.assertIsNotNone(captured.records[0].exc_info)


class ServerInactivityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 0.0
        self.enterContext(patch.object(server.time, "monotonic", side_effect=lambda: self.now))
        self.manager = EnvironmentManager()
        self.game = server.GameServer(
            self.manager, game_id="counter", game_alias="alias", inactivity_timeout_seconds=10,
        )
        self.addCleanup(self.game.stop)
        self.client = server.create_app(self.game).test_client()
        payload = self.client.post("/game/start", json={"game_id": "alias"}).get_json()
        self.headers = {"X-Session-Token": payload["session"]["token"]}

    def test_reads_and_rejected_requests_do_not_renew_idle_time(self) -> None:
        for at, path in ((2, "/health"), (4, "/game/last-step"), (6, "/game/environment-description")):
            self.now = at
            self.assertEqual(self.client.get(path, headers=self.headers).status_code, 200)
        self.now = 8
        self.assertEqual(self.client.post(
            "/game/action", json={"action": {"id": "UNKNOWN"}}, headers=self.headers,
        ).status_code, 400)
        self.now = 9
        self.assertEqual(self.client.post(
            "/game/action", json={"action": {"id": "INC"}},
        ).status_code, 400)
        self.now = 10
        # A request arriving at the deadline cannot revive an expired server.
        self.assertEqual(self.client.post(
            "/game/action", json={"action": {"id": "INC"}}, headers=self.headers,
        ).status_code, 503)
        self.assertEqual(self.manager.value, 0)
        self.assertFalse(self.game.failed)

    def test_moves_renew_idle_time_and_guard_remains_active_after_terminal(self) -> None:
        self.now = 9
        for _ in range(3):
            response = self.client.post(
                "/game/action", json={"action": {"id": "INC"}}, headers=self.headers,
            )
            self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["session"]["terminal"])
        self.now = 18
        self.assertEqual(self.client.get("/game/last-step", headers=self.headers).status_code, 200)
        self.now = 19
        self.assertEqual(self.client.get("/health").status_code, 503)

    def test_omitted_timeout_keeps_idle_server_available(self) -> None:
        game = server.GameServer(EnvironmentManager(), game_id="counter", game_alias="alias")
        self.addCleanup(game.stop)
        client = server.create_app(game).test_client()
        self.now = 10 * 3600
        self.assertEqual(client.post("/game/start", json={"game_id": "alias"}).status_code, 200)
        self.now += 10 * 3600
        self.assertEqual(client.get("/health").status_code, 200)

    def test_cli_rejects_nonpositive_or_nonfinite_timeout(self) -> None:
        for option in ("--inactivity-timeout-seconds", "--terminal-grace-seconds"):
            for value in ("0", "-1", "nan", "inf", "-inf", "invalid"):
                with self.subTest(option=option, value=value), patch("sys.stderr", new_callable=io.StringIO):
                    with self.assertRaises(SystemExit) as raised:
                        server.build_parser().parse_args([
                            "--environment", "example", "--game-id", "counter", "--game-alias", "alias",
                            option + "=" + value,
                        ])
                    self.assertEqual(raised.exception.code, 2)


class ServerTerminalGraceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 0.0
        self.manager = Mock()
        self.manager.start_environment.return_value = StartResult("Rules", Observation({}))
        self.manager.move.return_value = Observation({"state.txt": "done"}, terminal=True)

    def run_server(self, requests, *, grace=600, inactivity=None):
        http = MagicMock()
        http.__enter__.return_value = http
        request_index = 0

        def handle_request():
            nonlocal request_index
            self.manager.stop.assert_not_called()
            if request_index < len(requests):
                operation = requests[request_index]
                request_index += 1
                operation(make_server.call_args.args[2].test_client())
            else:
                self.assertLessEqual(http.handle_request.call_count, len(requests) + 2,
                                     "Server did not stop at its deadline")
                self.assertIsNotNone(http.timeout, "Server must wake without further requests")
                self.assertGreater(http.timeout, 0)
                self.now += http.timeout
            # Model the point where the HTTP response has finished sending.
            self.manager.stop.assert_not_called()

        http.handle_request.side_effect = handle_request
        args = ["--environment", "example", "--game-id", "counter", "--game-alias", "alias"]
        if grace is not None:
            args.extend(["--terminal-grace-seconds", str(grace)])
        if inactivity is not None:
            args.extend(["--inactivity-timeout-seconds", str(inactivity)])
        with (
            patch.object(server.time, "monotonic", side_effect=lambda: self.now),
            patch.object(server, "load_environment_manager", return_value=Mock(return_value=self.manager)),
            patch.object(server, "setup_logging"),
            patch.object(server, "make_server", return_value=http) as make_server,
            self.assertLogs(server.LOGGER, level="INFO") as logs,
        ):
            code = server.main(args)
        self.assertEqual(request_index, len(requests))
        self.manager.stop.assert_called_once_with()
        self.assertFalse(make_server.call_args.kwargs["threaded"])
        http.__exit__.assert_called_once()
        return code, http, "\n".join(logs.output)

    def test_terminal_start_or_move_gets_full_grace_after_response_and_reads_do_not_extend_it(self) -> None:
        for terminal_on_start in (True, False):
            with self.subTest(terminal_on_start=terminal_on_start):
                self.now = 0.0
                self.manager.reset_mock()
                headers = {}
                terminal_payload = None
                response_finished_at = None

                def start_environment(_game_id):
                    self.now += 10 * 3600
                    return StartResult("Rules", Observation({}, terminal=terminal_on_start))

                def move(_action):
                    self.now += 10 * 3600
                    return Observation({"state.txt": "done"}, terminal=True)

                self.manager.start_environment.side_effect = start_environment
                self.manager.move.side_effect = move

                def finish_terminal_response(payload):
                    nonlocal terminal_payload, response_finished_at
                    terminal_payload = payload
                    # Even a long final-response transfer precedes the grace period.
                    self.now += 3600
                    response_finished_at = self.now

                def start(client):
                    response = client.post("/game/start", json={"game_id": "alias"})
                    self.assertEqual(response.status_code, 200)
                    payload = response.get_json()
                    headers["X-Session-Token"] = payload["session"]["token"]
                    if terminal_on_start:
                        finish_terminal_response(payload)

                def terminal_move(client):
                    response = client.post("/game/action", json={"action": {"id": "INC"}}, headers=headers)
                    self.assertEqual(response.status_code, 200)
                    finish_terminal_response(response.get_json())

                def confirmations(client):
                    self.now += 599
                    response = client.get("/game/last-step", headers=headers)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.get_json(), terminal_payload)
                    self.assertTrue(response.get_json()["session"]["terminal"])
                    self.assertEqual(client.get("/game/environment-description", headers=headers).status_code, 200)
                    self.assertEqual(client.get("/health").status_code, 200)
                    rejected = client.post("/game/action", json={"action": {"id": "INC"}}, headers=headers)
                    self.assertEqual(rejected.status_code, 400)

                requests = [start, confirmations] if terminal_on_start else [start, terminal_move, confirmations]
                code, _http, logs = self.run_server(requests)
                self.assertEqual(code, 0)
                self.assertEqual(self.now, response_finished_at + 600)
                self.assertEqual(self.manager.move.call_count, 0 if terminal_on_start else 1)
                self.assertEqual(logs.count("terminal grace period started"), 1)
                self.assertIn("server terminal grace period expired", logs)

    def test_request_in_progress_finishes_before_terminal_shutdown(self) -> None:
        self.manager.start_environment.return_value = StartResult("Rules", Observation({}, terminal=True))
        headers = {}

        def start(client):
            payload = client.post("/game/start", json={"game_id": "alias"}).get_json()
            headers["X-Session-Token"] = payload["session"]["token"]

        def slow_response(client):
            self.now = 599
            response = client.get("/game/last-step", headers=headers)
            self.now += 2
            self.manager.stop.assert_not_called()
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["session"]["terminal"])

        code, http, _logs = self.run_server([start, slow_response])
        self.assertEqual(code, 0)
        self.assertEqual(http.handle_request.call_count, 2)
        self.assertEqual(self.now, 601)

    def test_earlier_deadline_wins_and_omitted_grace_preserves_inactivity_guard(self) -> None:
        self.manager.start_environment.return_value = StartResult("Rules", Observation({}, terminal=True))
        for inactivity, grace, expected, reason in (
            (1800, 600, 600, "terminal grace period expired"),
            (300, 600, 300, "inactivity timeout"),
            (300, None, 300, "inactivity timeout"),
        ):
            with self.subTest(inactivity=inactivity, grace=grace):
                self.now = 0.0
                self.manager.reset_mock()

                def start(client):
                    self.assertEqual(client.post("/game/start", json={"game_id": "alias"}).status_code, 200)

                def health(client):
                    self.now = expected - 0.5
                    self.assertEqual(client.get("/health").status_code, 200)

                code, _http, logs = self.run_server([start, health], grace=grace, inactivity=inactivity)
                self.assertEqual(code, 0)
                self.assertEqual(self.now, expected)
                self.assertIn(reason, logs)

    def test_grace_does_not_expire_without_a_terminal_observation(self) -> None:
        def start(client):
            self.assertEqual(client.post("/game/start", json={"game_id": "alias"}).status_code, 200)

        def still_running(client):
            self.now += 10 * 3600
            self.assertEqual(client.get("/health").status_code, 200)

        def end_test(_client):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.run_server([start, still_running, end_test])
        self.manager.stop.assert_called_once_with()


class ServerLifecycleTest(unittest.TestCase):
    def test_slow_initialization_and_move_each_get_a_full_idle_window_afterward(self) -> None:
        now = 0.0
        timeout = 2 * 3600
        duration = 10 * 3600
        calls = []

        class SlowManager(EnvironmentManager):
            def start_environment(self, game_id):
                nonlocal now
                calls.append(("start", threading.get_ident()))
                now += duration
                return super().start_environment(game_id)

            def move(self, action):
                nonlocal now
                calls.append(("move", threading.get_ident()))
                now += duration
                return super().move(action)

            def stop(self):
                calls.append(("stop", threading.get_ident()))
                super().stop()

        http = MagicMock()
        http.__enter__.return_value = http
        request_index = 0
        headers = {}

        def handle_request():
            nonlocal now, request_index, headers
            self.assertNotIn("stop", [name for name, _ in calls])
            client = make_server.call_args.args[2].test_client()
            if request_index == 0:
                response = client.post("/game/start", json={"game_id": "alias"})
                self.assertEqual(response.status_code, 200)
                headers = {"X-Session-Token": response.get_json()["session"]["token"]}
            elif request_index == 1:
                now += timeout - 1
                response = client.post("/game/action", json={"action": {"id": "INC"}}, headers=headers)
                self.assertEqual(response.status_code, 200)
            elif request_index == 2:
                now += timeout - 1
                self.assertEqual(client.get("/health").status_code, 200)
            elif request_index == 3:
                # No new request: the HTTP wait wakes up when idle time runs out.
                self.assertEqual(http.timeout, 1)
                now += http.timeout
            else:
                self.fail("Server did not stop after the idle deadline")
            request_index += 1

        http.handle_request.side_effect = handle_request
        with (
            patch.object(server.time, "monotonic", side_effect=lambda: now),
            patch.object(server, "load_environment_manager", return_value=SlowManager),
            patch.object(server, "setup_logging"),
            patch.object(server, "make_server", return_value=http) as make_server,
        ):
            self.assertEqual(server.main([
                "--environment", "example", "--game-id", "counter", "--game-alias", "alias",
                "--inactivity-timeout-seconds", str(timeout),
            ]), 0)
        self.assertEqual(request_index, 4)
        self.assertEqual([name for name, _ in calls], ["start", "move", "stop"])
        self.assertEqual({ident for _, ident in calls}, {threading.get_ident()})
        self.assertFalse(make_server.call_args.kwargs["threaded"])
        http.__exit__.assert_called_once()

    def test_idle_timeout_cleans_up_without_a_start_request_and_preserves_cleanup_errors(self) -> None:
        for cleanup_fails in (False, True):
            with self.subTest(cleanup_fails=cleanup_fails):
                now = 0.0
                manager = Mock()
                if cleanup_fails:
                    manager.stop.side_effect = RuntimeError("Cleanup failed")
                http = MagicMock()
                http.__enter__.return_value = http

                def handle_request():
                    nonlocal now
                    self.assertEqual(http.timeout, 0.5)
                    now += http.timeout

                http.handle_request.side_effect = handle_request
                with (
                    patch.object(server.time, "monotonic", side_effect=lambda: now),
                    patch.object(server, "load_environment_manager", return_value=Mock(return_value=manager)),
                    patch.object(server, "setup_logging"),
                    patch.object(server, "make_server", return_value=http),
                    self.assertLogs(server.LOGGER, level="INFO") as logs,
                ):
                    code = server.main([
                        "--environment", "example", "--game-id", "counter", "--game-alias", "alias",
                        "--inactivity-timeout-seconds", "0.5",
                    ])
                self.assertEqual(code, int(cleanup_fails))
                manager.stop.assert_called_once_with()
                manager.start_environment.assert_not_called()
                http.handle_request.assert_called_once_with()
                self.assertIn("server inactivity timeout", "\n".join(logs.output))

    def test_main_initializes_moves_and_stops_on_one_thread(self) -> None:
        for fatal in (False, True):
            with self.subTest(fatal=fatal):
                calls = []

                class RecordingManager(EnvironmentManager):
                    def __init__(self, parameters):
                        calls.append(("init", threading.get_ident()))
                        super().__init__(parameters)

                    def start_environment(self, game_id):
                        calls.append(("start", threading.get_ident()))
                        return super().start_environment(game_id)

                    def move(self, action):
                        calls.append(("move", threading.get_ident()))
                        if fatal:
                            raise FatalEnvironmentError("Backend closed")
                        return super().move(action)

                    def stop(self):
                        calls.append(("stop", threading.get_ident()))
                        super().stop()

                http = MagicMock()
                http.__enter__.return_value = http
                request_index = 0
                headers = {}

                def handle_request():
                    nonlocal request_index, headers
                    client = make_server.call_args.args[2].test_client()
                    if request_index == 0:
                        payload = client.post("/game/start", json={"game_id": "alias"}).get_json()
                        headers = {"X-Session-Token": payload["session"]["token"]}
                    elif request_index == 1:
                        response = client.post("/game/action", json={"action": {"id": "INC"}}, headers=headers)
                        self.assertEqual(response.status_code, 503 if fatal else 200)
                    else:
                        raise KeyboardInterrupt
                    request_index += 1

                http.handle_request.side_effect = handle_request
                with patch.object(server, "load_environment_manager", return_value=RecordingManager), patch.object(
                    server, "setup_logging"
                ), patch.object(server, "make_server", return_value=http) as make_server:
                    args = ["--environment", "example", "--game-id", "counter", "--game-alias", "alias"]
                    if fatal:
                        with self.assertLogs(server.LOGGER, level="ERROR"):
                            self.assertEqual(server.main(args), 1)
                    else:
                        with self.assertRaises(KeyboardInterrupt):
                            server.main(args)
                self.assertEqual([name for name, _ in calls], ["init", "start", "move", "stop"])
                self.assertEqual({ident for _, ident in calls}, {threading.get_ident()})
                self.assertFalse(make_server.call_args.kwargs["threaded"])
                http.__exit__.assert_called_once()


class ObservationTest(unittest.TestCase):
    def test_unicode_and_binary_are_encoded_without_changing_bytes(self) -> None:
        raw = {"state.txt": "Hello, 世界!\n", "data.bin": bytes(range(256))}
        encoded = serialize_observation(Observation(raw))
        self.assertEqual(
            {item["name"]: base64.b64decode(item["content_base64"]) for item in encoded},
            {"state.txt": raw["state.txt"].encode("utf-8"), "data.bin": raw["data.bin"]},
        )

    def test_unsafe_names_and_unsupported_values_are_rejected(self) -> None:
        for name in ("", ".", "..", "../escape", "/absolute", "nested/file", "nested\\file", "nul\0", 4):
            with self.subTest(name=name), self.assertRaises(ValueError):
                serialize_observation(Observation({name: "text"}))
        with self.assertRaises(TypeError):
            serialize_observation(Observation({"state.json": {"value": 1}}))


if __name__ == "__main__":
    unittest.main()
