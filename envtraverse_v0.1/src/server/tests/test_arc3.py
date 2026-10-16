from __future__ import annotations

from contextlib import contextmanager
from io import BytesIO
import base64
import json
import logging
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server
from artifacts import serialize_observation
from environment_api import EnvironmentError, EnvironmentStartError, FatalEnvironmentError, InvalidActionError

try:
    from environments.arc3 import environment_manager as arc3
    from PIL import Image
except ModuleNotFoundError as exc:
    if exc.name not in {"arc_agi", "arcengine", "numpy", "PIL"}:
        raise
    arc3 = None


def frame(state="NOT_FINISHED", frames=None):
    return SimpleNamespace(
        state=SimpleNamespace(name=state),
        levels_completed=1 if state == "WIN" else 0,
        win_levels=1,
        available_actions=[1, 6],
        action_input=None,
        frame=frames if frames is not None else [[[0, 1], [14, 15]]],
    )


@unittest.skipIf(arc3 is None, "ARC3 dependencies are not installed")
class ARC3ParametersTest(unittest.TestCase):
    def test_defaults_and_boolean_competition_are_preserved(self) -> None:
        for raw in (None, {}):
            with self.subTest(parameters=raw):
                parameters = arc3.EnvironmentManagerParameters.from_dict(raw)
                self.assertIs(parameters.competition, False)
                self.assertEqual(parameters.source_url, arc3.DEFAULT_SOURCE_URL)
                self.assertEqual(parameters.tags, arc3.DEFAULT_SCORECARD_TAGS)
                self.assertIsNot(parameters.tags, arc3.DEFAULT_SCORECARD_TAGS)
        for competition in (False, True):
            with self.subTest(competition=competition):
                parameters = arc3.EnvironmentManagerParameters.from_dict({"competition": competition})
                self.assertIs(parameters.competition, competition)

    def test_parameters_must_be_a_dictionary(self) -> None:
        for raw in (False, True, 0, 1, "", "false", [], ["competition"]):
            with self.subTest(parameters=raw), self.assertRaisesRegex(TypeError, "dictionary"):
                arc3.EnvironmentManagerParameters.from_dict(raw)

    def test_competition_requires_a_boolean_without_coercion(self) -> None:
        for value in ("false", "true", "no", "0", "", 0, 1, 0.0, 1.0, None, [], {}):
            with self.subTest(competition=value), self.assertRaisesRegex(TypeError, "competition.*boolean"):
                arc3.EnvironmentManagerParameters.from_dict({"competition": value})

    def test_unknown_parameters_are_rejected(self) -> None:
        for name in ("competiton", "tgas", "source_urll", "seed", "ARC_API_KEY"):
            with self.subTest(parameter=name), self.assertRaisesRegex(ValueError, f"Unknown.*{name}"):
                arc3.EnvironmentManagerParameters.from_dict({"competition": False, name: "unused"})

    def test_source_url_requires_a_string(self) -> None:
        for value in (None, False, 42, 1.5, [], {}):
            with self.subTest(source_url=value), self.assertRaisesRegex(TypeError, "source_url.*string"):
                arc3.EnvironmentManagerParameters.from_dict({"source_url": value})

    def test_tags_and_alias_keep_defaults_and_string_normalization(self) -> None:
        for key in ("tags", "tag"):
            for value, expected in (
                (None, arc3.DEFAULT_SCORECARD_TAGS),
                ("", arc3.DEFAULT_SCORECARD_TAGS),
                ("  ", arc3.DEFAULT_SCORECARD_TAGS),
                ([], arc3.DEFAULT_SCORECARD_TAGS),
                (["", "  "], arc3.DEFAULT_SCORECARD_TAGS),
                (" custom ", ["custom"]),
                ([" first ", "", " second "], ["first", "second"]),
            ):
                with self.subTest(parameter=key, value=value):
                    original = value.copy() if isinstance(value, list) else value
                    parameters = arc3.EnvironmentManagerParameters.from_dict({key: value})
                    self.assertEqual(parameters.tags, expected)
                    self.assertEqual(value, original)
                    self.assertIsNot(parameters.tags, arc3.DEFAULT_SCORECARD_TAGS)

    def test_tag_values_are_not_coerced_to_strings(self) -> None:
        for key in ("tags", "tag"):
            for value in (False, True, 42, 1.5, {}, ("tag",), [42], [None], [False], [["tag"]], ["valid", 42]):
                with self.subTest(parameter=key, value=value), self.assertRaisesRegex(TypeError, "tags.*string"):
                    arc3.EnvironmentManagerParameters.from_dict({key: value})

    def test_tags_and_tag_cannot_both_be_specified(self) -> None:
        for raw in (
            {"tags": ["first"], "tag": "second"},
            {"tags": "same", "tag": "same"},
            {"tags": None, "tag": "first"},
        ):
            with self.subTest(parameters=raw), self.assertRaisesRegex(ValueError, "only one"):
                arc3.EnvironmentManagerParameters.from_dict(raw)

    def test_invalid_parameters_fail_before_initializing_backend(self) -> None:
        with patch.object(arc3, "Arcade", side_effect=AssertionError("Backend must not be initialized")) as factory:
            for raw in (
                {"competition": "false"}, {"competiton": True}, {"source_url": 42},
                {"tags": ["valid", False]}, {"tag": "first", "tags": "second"},
            ):
                with self.subTest(parameters=raw), self.assertRaises((TypeError, ValueError)):
                    arc3.EnvironmentManager(raw)
            factory.assert_not_called()


@unittest.skipIf(arc3 is None, "ARC3 dependencies are not installed")
class ARC3Test(unittest.TestCase):
    PRIVATE_GAME_ID = "secret-game-version"

    def setUp(self) -> None:
        self.arcade = Mock()
        self.environment = Mock()
        self.environment.observation_space = frame()
        self.arcade.make.return_value = self.environment
        patcher = patch.object(arc3, "Arcade", return_value=self.arcade)
        self.backend_factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.manager = arc3.EnvironmentManager()
        self.addCleanup(self.manager.stop)

    def assert_neutral(self, text: str) -> None:
        self.assertNotIn("arc", text.lower())
        self.assertNotIn(self.PRIVATE_GAME_ID, text)

    def start_game(self, game_id):
        result = self.manager.start_environment(game_id)
        return result

    @contextmanager
    def http_client(self, parameters=None):
        manager = arc3.EnvironmentManager(parameters or {})
        game = server.GameServer(manager, game_id=self.PRIVATE_GAME_ID, game_alias="public-alias")
        try:
            yield server.create_app(game).test_client()
        finally:
            game.stop()

    def assert_neutral_response(self, response, status=200) -> dict:
        self.assertEqual(response.status_code, status)
        payload = response.get_json()
        public_text = json.loads(json.dumps(payload))
        # Base64-encoded image bytes can coincidentally spell "arc"; inspect
        # decoded content instead of searching the transport encoding.
        for artifact in public_text.get("step", {}).get("artifacts", []):
            content = base64.b64decode(artifact.pop("content_base64"))
            if artifact["name"].endswith(".txt"):
                self.assert_neutral(content.decode("utf-8"))
            elif artifact["name"].endswith(".png"):
                with Image.open(BytesIO(content)) as image:
                    self.assert_neutral(json.dumps(image.info))
        self.assert_neutral(json.dumps(public_text))
        return payload

    def test_start_supplies_description_ascii_and_png_bytes(self) -> None:
        result = self.start_game("test-game")
        self.assertEqual(result.environment_description, arc3.DESCRIPTION_PATH.read_text(encoding="utf-8"))
        self.assertEqual(result.observation.files["final.txt"], "01\nEF\n")
        with Image.open(BytesIO(result.observation.files["final.png"])) as image:
            self.assertEqual(image.size, (16, 16))
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
        metadata = json.loads(result.observation.files["metadata.txt"])
        self.assertEqual(metadata["available_actions"], ["ACTION1", "ACTION6"])
        self.assertFalse(result.observation.terminal)

    def test_coordinate_move_preserves_intermediate_frames_and_terminal_state(self) -> None:
        started = self.start_game("test-game")
        self.environment.step.return_value = frame("WIN", [[[1]], [[2]]])
        result = self.manager.move(action={"id": "ACTION6", "data": {"x": 12, "y": 34}})
        self.environment.step.assert_called_once_with(arc3.GameAction.ACTION6, data={"x": 12, "y": 34})
        self.assertTrue(result.terminal)
        self.assertEqual(result.files["intermediate_00.txt"], "1\n")
        self.assertEqual(result.files["final.txt"], "2\n")
        self.assertIn("intermediate_00.png", result.files)
        self.assertEqual(
            json.loads(result.files["metadata.txt"])["action_input"],
            {"id": "ACTION6", "data": {"x": 12, "y": 34}},
        )

    def test_missing_game_cannot_be_retried(self) -> None:
        self.arcade.make.return_value = None
        with self.assertRaises(EnvironmentStartError):
            self.manager.start_environment("missing")
        with self.assertRaises(EnvironmentStartError):
            self.manager.start_environment("test-game")
        self.arcade.make.assert_called_once()
        self.assertIsNone(self.manager.environment)

    def test_missing_frame_raises_fatal_error(self) -> None:
        self.start_game("test-game")
        self.environment.step.return_value = None
        with self.assertRaises(FatalEnvironmentError):
            self.manager.move({"id": "ACTION1"})

    def test_failed_render_stops_manager_before_raising(self) -> None:
        with patch.object(arc3, "build_files", side_effect=ValueError("Invalid frame")), patch.object(
            self.manager, "stop", wraps=self.manager.stop
        ) as stop:
            with self.assertRaises(ValueError):
                self.manager.start_environment("test-game")
        stop.assert_called_once()
        self.assertIsNone(self.manager.environment)
        self.assertIsNone(self.manager.arc)
        self.arcade.close_scorecard.assert_not_called()

    def test_one_scorecard_is_opened_and_closed_for_the_game(self) -> None:
        self.arcade.create_scorecard.return_value = "test-scorecard"
        self.arcade.close_scorecard.return_value = "final score"
        with tempfile.TemporaryDirectory() as directory:
            scorecard_path = Path(directory) / "scorecard.txt"
            final_path = Path(directory) / "final.txt"
            with patch.object(arc3, "SCORECARD_PATH", scorecard_path), patch.object(
                arc3, "FINAL_SCORECARD_PATH", final_path
            ):
                manager = arc3.EnvironmentManager({"competition": True})
                self.arcade.create_scorecard.assert_not_called()
                manager.start_environment("one")
                self.arcade.make.assert_called_once_with("one", scorecard_id="test-scorecard")
                with self.assertRaises(EnvironmentStartError):
                    manager.start_environment("two")
                self.arcade.close_scorecard.assert_not_called()
                manager.stop()
                manager.stop()
                self.arcade.close_scorecard.assert_called_once_with("test-scorecard")
                self.assertEqual(scorecard_path.read_text(), "test-scorecard\n")
                self.assertEqual(final_path.read_text(), "final score")
                self.assertIsNone(manager.environment)
                self.assertIsNone(manager.arc)

    def test_failed_competition_start_finalizes_its_scorecard(self) -> None:
        self.arcade.create_scorecard.return_value = "test-scorecard"
        self.arcade.make.side_effect = RuntimeError("Backend failed")
        with tempfile.TemporaryDirectory() as directory, patch.object(
            arc3, "SCORECARD_PATH", Path(directory) / "scorecard.txt"
        ), patch.object(arc3, "FINAL_SCORECARD_PATH", Path(directory) / "final.txt"):
            manager = arc3.EnvironmentManager({"competition": True})
            with self.assertRaises(EnvironmentStartError):
                manager.start_environment("one")
            manager.stop()
            self.arcade.close_scorecard.assert_called_once_with("test-scorecard")
            self.assertIsNone(manager.environment)

    def test_constructor_and_stop_before_start_do_not_initialize_backend(self) -> None:
        self.backend_factory.assert_not_called()
        with self.assertRaises(EnvironmentError):
            self.manager.move({"id": "ACTION1"})
        self.manager.stop()
        self.manager.stop()
        with self.assertRaises(EnvironmentStartError):
            self.manager.start_environment("one")
        self.backend_factory.assert_not_called()

    def test_game_reset_rules_and_level_counters_are_preserved(self) -> None:
        self.start_game("test-game")
        game = self.manager
        with self.assertRaises(InvalidActionError):
            game.move({"id": "RESET"})
        self.environment.reset.assert_not_called()
        self.environment.step.return_value = frame("GAME_OVER")
        game.move({"id": "ACTION1"})
        self.assertFalse(game.terminal)
        with self.assertRaises(InvalidActionError):
            game.move({"id": "ACTION1"})
        self.environment.reset.return_value = frame()
        game.move({"id": "RESET"})
        self.assertEqual((game.current_level_steps, game.current_attempt_steps), (2, 0))
        self.assertEqual(game.state_name, "NOT_FINISHED")
        next_level = frame()
        next_level.levels_completed = 1
        next_level.win_levels = 2
        self.environment.step.return_value = next_level
        game.move({"id": "ACTION1"})
        self.assertEqual((game.current_level_steps, game.current_attempt_steps, game.levels_completed), (0, 0, 1))

    def test_game_step_budget_ends_the_single_game(self) -> None:
        self.start_game("one")
        self.environment.step.return_value = frame()
        with patch.object(arc3, "MAX_STEPS_PER_LEVEL", 1):
            self.assertFalse(self.manager.move({"id": "ACTION1"}).terminal)
            self.assertTrue(self.manager.move({"id": "ACTION1"}).terminal)
            with self.assertRaises(InvalidActionError):
                self.manager.move({"id": "ACTION1"})
        self.assertEqual(self.manager.current_level_steps, 2)

    def test_start_errors_do_not_identify_backend_or_game(self) -> None:
        backend_error = RuntimeError(f"ARC3 failed to load {self.PRIVATE_GAME_ID}")
        for result, error in ((None, None), (None, backend_error), (self.environment, None)):
            with self.subTest(result=result, error=error):
                self.arcade.make.side_effect = error
                self.arcade.make.return_value = result
                self.environment.observation_space = None
                with self.assertRaises(EnvironmentStartError) as raised:
                    manager = arc3.EnvironmentManager()
                    self.addCleanup(manager.stop)
                    manager.start_environment(self.PRIVATE_GAME_ID)
                self.assert_neutral(str(raised.exception))
                self.assertIs(raised.exception.__cause__, error)

    def test_action_errors_do_not_echo_identifying_input(self) -> None:
        started = self.start_game(self.PRIVATE_GAME_ID)
        for action in (
            {"id": f"ARC3-{self.PRIVATE_GAME_ID}"},
            {"id": "ACTION6", "data": {"x": "ARC", "y": 1}},
        ):
            with self.subTest(action=action), self.assertRaises(ValueError) as raised:
                self.manager.move(action)
            self.assert_neutral(str(raised.exception))
        self.environment.step.return_value = None
        with self.assertRaises(FatalEnvironmentError) as raised:
            self.manager.move({"id": "ACTION1"})
        self.assert_neutral(str(raised.exception))

    def test_decoded_observations_preserve_environment_action_data(self) -> None:
        observation = frame()
        observation.game_id = self.PRIVATE_GAME_ID
        observation.guid = "ARC-private-guid"
        observation.source_url = arc3.DEFAULT_SOURCE_URL
        observation.action_input = SimpleNamespace(
            id=arc3.GameAction.ACTION6,
            data={
                "x": 12,
                "y": 34,
                "game_id": self.PRIVATE_GAME_ID,
                "backend": "ARC3",
                "source_url": arc3.DEFAULT_SOURCE_URL,
            },
            reasoning={"backend": "ARC3", "game": self.PRIVATE_GAME_ID},
        )
        self.environment.observation_space = observation
        started = self.start_game(self.PRIVATE_GAME_ID)
        self.assert_neutral(started.environment_description)
        artifacts = serialize_observation(started.observation)
        metadata_artifact = next(artifact for artifact in artifacts if artifact["name"] == "metadata.txt")
        metadata = json.loads(base64.b64decode(metadata_artifact["content_base64"]))
        self.assertEqual(metadata["action_input"], {"id": "ACTION6", "data": observation.action_input.data})
        self.assertEqual(
            set(metadata), {"state", "levels_completed", "win_levels", "available_actions", "action_input"}
        )
        observation.action_input.id = arc3.GameAction.ACTION1
        self.environment.step.return_value = observation
        moved = self.manager.move({"id": "ACTION1"})
        self.assertEqual(
            json.loads(moved.files["metadata.txt"])["action_input"],
            {"id": "ACTION1", "data": observation.action_input.data},
        )

    def test_environment_metadata_is_not_validated_or_redacted(self) -> None:
        for field, value, expected in (
            ("state", SimpleNamespace(name="ARC3-" + self.PRIVATE_GAME_ID), "ARC3-" + self.PRIVATE_GAME_ID),
            ("levels_completed", "ARC3", "ARC3"),
            ("win_levels", None, None),
            ("available_actions", [self.PRIVATE_GAME_ID], [self.PRIVATE_GAME_ID]),
            (
                "action_input",
                SimpleNamespace(id="ARC-custom-action", data="ARC-custom-data"),
                {"id": "ARC-custom-action", "data": "ARC-custom-data"},
            ),
            *(
                (
                    "action_input",
                    SimpleNamespace(id=arc3.GameAction.ACTION6, data=data),
                    {"id": "ACTION6", "data": data},
                )
                for data in (None, {}, {"x": "ARC", "y": -1}, {"x": 64, "y": "34"})
            ),
        ):
            observation = frame()
            setattr(observation, field, value)
            with self.subTest(field=field, value=value):
                metadata = json.loads(arc3.build_files(observation)["metadata.txt"])
                self.assertEqual(metadata[field], expected)

    def test_backend_logs_keep_identifiers_and_tracebacks(self) -> None:
        self.assertEqual(arc3.LOGGER.name, "env-server.arc3")
        self.start_game("test-game")
        self.backend_factory.assert_called_once_with()
        for logger in (arc3.LOGGER, logging.getLogger("arc_agi.base"), logging.getLogger("arc_agi.scorecard")):
            with self.subTest(logger=logger.name), self.assertLogs(logger, level=logging.INFO) as captured:
                logger.info("ARC3 opened %s", self.PRIVATE_GAME_ID)
                try:
                    raise RuntimeError(f"ARC3 backend error for {self.PRIVATE_GAME_ID}")
                except RuntimeError:
                    logger.exception("ARC3 returned a failure")
            self.assertEqual(len(captured.records), 2)
            self.assertIn(f"ARC3 opened {self.PRIVATE_GAME_ID}", captured.output[0])
            self.assertIn(f"ARC3 backend error for {self.PRIVATE_GAME_ID}", captured.output[1])
            self.assertIn("Traceback", captured.output[1])
            self.assertIsNotNone(captured.records[1].exc_info)

    def test_source_url_default_and_override(self) -> None:
        self.assertEqual(arc3.DEFAULT_SOURCE_URL, "https://github.com/astroseger/arc-3-agents-baseline1")
        self.assertEqual(self.manager.parameters.source_url, arc3.DEFAULT_SOURCE_URL)
        self.assertEqual(arc3.EnvironmentManagerParameters().source_url, arc3.DEFAULT_SOURCE_URL)
        for parameters in (None, {}, {"source_url": ""}, {"source_url": "  "}):
            with self.subTest(parameters=parameters):
                self.assertEqual(
                    arc3.EnvironmentManagerParameters.from_dict(parameters).source_url, arc3.DEFAULT_SOURCE_URL
                )
        self.assertEqual(
            arc3.EnvironmentManagerParameters.from_dict({"source_url": " https://example.com/source "}).source_url,
            "https://example.com/source",
        )

    def test_scorecard_configuration_is_not_added_to_observations(self) -> None:
        self.arcade.create_scorecard.return_value = "ARC-private-scorecard"
        self.arcade.close_scorecard.return_value = json.dumps(
            {"source_url": arc3.DEFAULT_SOURCE_URL, "game_id": self.PRIVATE_GAME_ID}
        )
        observation = frame()
        observation.game_id = self.PRIVATE_GAME_ID
        observation.action_input = SimpleNamespace(
            id=arc3.GameAction.ACTION6,
            data={"x": 12, "y": 34},
        )
        self.environment.observation_space = observation
        self.environment.step.return_value = observation
        with tempfile.TemporaryDirectory() as directory:
            scorecard_path = Path(directory) / "scorecard.txt"
            final_path = Path(directory) / "final_scorecard.txt"
            with patch.object(arc3, "SCORECARD_PATH", scorecard_path), patch.object(
                arc3, "FINAL_SCORECARD_PATH", final_path
            ), self.assertLogs(arc3.LOGGER, level=logging.INFO) as captured:
                with self.http_client({"competition": True}) as client:
                    self.arcade.create_scorecard.assert_not_called()
                    started = self.assert_neutral_response(
                        client.post("/game/start", json={"game_id": "public-alias"})
                    )
                    self.arcade.create_scorecard.assert_called_once_with(
                        source_url=arc3.DEFAULT_SOURCE_URL, tags=arc3.DEFAULT_SCORECARD_TAGS
                    )
                    headers = {"X-Session-Token": started["session"]["token"]}
                    self.assert_neutral_response(client.get("/game/environment-description", headers=headers))
                    self.assertEqual(
                        self.assert_neutral_response(client.get("/game/last-step", headers=headers)), started
                    )
                    moved = self.assert_neutral_response(
                        client.post(
                            "/game/action",
                            headers=headers,
                            json={"action": {"id": "ACTION6", "data": {"x": 12, "y": 34}}},
                        )
                    )
                    self.assertEqual(
                        self.assert_neutral_response(client.get("/game/last-step", headers=headers)), moved
                    )
                self.assertEqual(scorecard_path.read_text(), "ARC-private-scorecard\n")
                self.assertIn(arc3.DEFAULT_SOURCE_URL, final_path.read_text())
                self.assertIn(self.PRIVATE_GAME_ID, final_path.read_text())
            self.assertIn("opened scorecard: ARC-private-scorecard", "\n".join(captured.output))
            self.assertIn("closing scorecard: ARC-private-scorecard", "\n".join(captured.output))
            self.assertIn(str(final_path), "\n".join(captured.output))

    def test_sdk_start_exception_is_logged_but_not_returned_through_http(self) -> None:
        backend_error = RuntimeError(f"ARC3 failed to load {self.PRIVATE_GAME_ID}")
        self.arcade.make.side_effect = backend_error
        with self.http_client() as client, self.assertLogs(server.LOGGER, level=logging.INFO) as captured:
            response = client.post("/game/start", json={"game_id": "public-alias"})
        self.assertEqual(self.assert_neutral_response(response, 400), {"error": "Failed to create environment"})
        self.assertIn(str(backend_error), "\n".join(captured.output))
        self.assertIn("Traceback", "\n".join(captured.output))

    def test_sdk_step_exception_is_logged_but_not_returned_through_http(self) -> None:
        with self.http_client() as client, self.assertLogs(server.LOGGER, level=logging.INFO) as captured:
            started = self.assert_neutral_response(client.post("/game/start", json={"game_id": "public-alias"}))
            self.environment.step.side_effect = RuntimeError(
                f"ARC3 / backend connection failed for {self.PRIVATE_GAME_ID}"
            )
            response = client.post(
                "/game/action",
                headers={"X-Session-Token": started["session"]["token"]},
                json={"action": {"id": "ACTION1"}},
            )
        self.assertEqual(self.assert_neutral_response(response, 500), {"error": "Environment request failed"})
        log_text = "\n".join(captured.output)
        self.assertIn(f"game_id={self.PRIVATE_GAME_ID}", log_text)
        self.assertIn(f"ARC3 / backend connection failed for {self.PRIVATE_GAME_ID}", log_text)
        self.assertIn("Traceback", log_text)

    def test_invalid_action_and_missing_frame_errors_are_neutral_through_http(self) -> None:
        with self.http_client() as client:
            started = self.assert_neutral_response(client.post("/game/start", json={"game_id": "public-alias"}))
            headers = {"X-Session-Token": started["session"]["token"]}
            for action in (
                {"id": f"ARC3-{self.PRIVATE_GAME_ID}"},
                {"id": "ACTION6", "data": {"x": "ARC", "y": 1}},
            ):
                with self.subTest(action=action), self.assertLogs(server.LOGGER, level=logging.WARNING):
                    self.assert_neutral_response(client.post("/game/action", headers=headers, json={"action": action}), 400)
            self.environment.step.return_value = None
            with self.assertLogs(server.LOGGER, level=logging.ERROR):
                response = client.post("/game/action", headers=headers, json={"action": {"id": "ACTION1"}})
            self.assertEqual(
                self.assert_neutral_response(response, 503),
                {"error": "Environment returned no frame; the backend may have closed the session"},
            )
            self.assertEqual(client.get("/health").status_code, 503)

    def test_description_and_workspace_helpers_use_neutral_language(self) -> None:
        self.assert_neutral(arc3.DESCRIPTION_PATH.read_text(encoding="utf-8"))
        workspace_files = arc3.DESCRIPTION_PATH.parent / "to_agent_workspace"
        for path in workspace_files.iterdir():
            if path.is_file():
                self.assert_neutral(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
