from __future__ import annotations

import base64
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


class ServerLifecycleTest(unittest.TestCase):
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
