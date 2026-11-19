import json
from uuid import uuid4

import pytest

from ..actions import validate_action
from ..compat import EnvironmentStartError, FatalEnvironmentError, InvalidActionError
from ..description import environment_description
from ..catalog import get_task
from ..environment_manager import EnvironmentManager
from ..protocol import RuntimeClient, RuntimeFault, decode_observation, observation
from ..settings import Settings


@pytest.mark.parametrize("action", [
    {"id": "key_down", "key": "t"},
    {"id": "key_down", "key": "w", "arbitrary": 1},
    {"id": "key_press", "key": "w", "duration_ms": True},
    {"id": "key_press", "key": "w", "duration_ms": 10001},
    {"id": "mouse_move", "x": 2},
    {"id": "mouse_move", "x": 2, "y": 3, "dx": 1},
    {"id": "mouse_move", "x": 960, "y": 0},
    {"id": "mouse_move", "dx": False},
    {"id": "mouse_click", "button": "left", "y": 3},
    {"id": "mouse_scroll", "amount": 11},
    {"id": "release_all", "keys": []},
])
def test_entire_malformed_action_rejected(action):
    with pytest.raises(RuntimeFault) as error:
        validate_action(action)
    assert error.value.code == "invalid_action"


def test_action_defaults_without_mutating_request():
    original = {"id": "key_press", "key": "w"}
    assert validate_action(original)["duration_ms"] == 250
    assert "duration_ms" not in original
    assert validate_action({"id": "mouse_move", "dy": -5}) == {"id": "mouse_move", "dx": 0, "dy": -5}


@pytest.mark.parametrize("settings", [
    {"timeout": 1}, {"minecraft_task_timeout_seconds": 0}, {"minecraft_task_timeout_seconds": float("nan")},
    {"minecraft_task_timeout_seconds": True}, {"record_fps": -0.5}, {"record_fps": 31},
    {"capture_fps": 61}, {"capture_fps": 0.5, "record_fps": 0}, {"width": True},
    {"hardcore": True}, {"seed": "123"},
])
def test_settings_reject_invalid_values(settings):
    with pytest.raises(ValueError):
        Settings.from_dict(settings)


class FakeClient:
    socket_path = "/unused/runtime.sock"

    def __init__(self, session_id):
        self.session_id = session_id
        self.calls = []
        self.terminal = False
        self.error = None

    def call(self, method, **parameters):
        self.calls.append((method, parameters))
        if self.error:
            raise self.error
        if method == "start":
            return {"session_id": self.session_id, "environment_description": "Play Minecraft",
                    "observation": observation({"ready.json": '{"ready":true}'})}
        return {"observation": observation({"action.json": json.dumps({"accepted": not self.terminal})}, terminal=self.terminal)}


def manager():
    result = EnvironmentManager({"session_id": uuid4().hex})
    result.client = FakeClient(result.session_id)
    return result


def test_manager_uses_action_response_for_terminal_without_server_polling():
    adapter = manager()
    adapter.start_environment("craft_wooden_pickaxe")
    adapter.client.terminal = True
    result = adapter.move({"id": "key_down", "key": "w"})
    assert result.terminal
    assert json.loads(result.files["action.json"])["accepted"] is False
    assert adapter.client.calls[-1][0] == "action"
    with pytest.raises(InvalidActionError):
        adapter.move({"id": "release_all"})


def test_manager_prevalidates_without_sending_invalid_inputs():
    adapter = manager()
    adapter.start_environment("craft_wooden_pickaxe")
    with pytest.raises(InvalidActionError):
        adapter.move({"id": "mouse_move", "x": 960, "y": 0})
    assert [method for method, _ in adapter.client.calls] == ["start"]


def test_connection_failure_does_not_retry_an_action():
    adapter = manager()
    adapter.start_environment("craft_wooden_pickaxe")
    adapter.client.error = ConnectionError("lost response after applied input")
    with pytest.raises(FatalEnvironmentError):
        adapter.move({"id": "key_down", "key": "w"})
    assert [method for method, _ in adapter.client.calls].count("action") == 1


def test_duplicate_start_does_not_close_another_owners_session(monkeypatch):
    adapter = manager()
    adapter.client.error = RuntimeFault("duplicate_session", "Already exists")
    close_calls = []
    monkeypatch.setattr(RuntimeClient, "call", lambda *args, **kwargs: close_calls.append(args))
    with pytest.raises(EnvironmentStartError):
        adapter.start_environment("craft_wooden_pickaxe")
    assert not close_calls
    adapter.stop()


def test_peaceful_rejects_hostile_task_before_starting_runtime():
    adapter = EnvironmentManager({"difficulty": "peaceful"})
    adapter.client = FakeClient(adapter.session_id)
    with pytest.raises(EnvironmentStartError, match="hostile"):
        adapter.start_environment("kill_zombie")
    assert not adapter.client.calls


def test_protocol_roundtrip_and_public_description_are_pixel_only():
    encoded = observation({"action.json": '{"accepted":true}', "binary.bin": b"abc"}, terminal=True)
    files, terminal = decode_observation(encoded)
    assert files["binary.bin"] == b"abc" and terminal
    text = environment_description(get_task("craft_wooden_pickaxe"), Settings())
    assert "latest_frame.jpg" in text and "minecraft_task_timeout_seconds" in text
    assert "key_down" in text and "mouse_click" in text
    assert "telemetry/state.json" not in text
