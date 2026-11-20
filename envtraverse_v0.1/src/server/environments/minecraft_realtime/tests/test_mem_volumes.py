"""RAM publication is optional, session-scoped, and leaves recordings intact."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import time
from uuid import uuid4

import pytest

from minecraft_realtime.environment_manager import EnvironmentManager
from minecraft_realtime.docker_backend import DockerBackend
from minecraft_realtime.protocol import RuntimeFault, observation
from minecraft_realtime.recording import FramePublisher
from minecraft_realtime.resources import MEMORY_FRAME_PATH, WORKSPACE_FRAME_PATH
from minecraft_realtime.runtime import RuntimeService
from minecraft_realtime.settings import Settings
from minecraft_realtime.tests.test_runtime import Clock, FakeBackend, FakeNetwork, JPEG, prepare_workspace, request


def resources(session_id, mount="/latest_frame"):
    return {"session_id": session_id, "mem_disk_mounts": [{
        "mount": mount, "volume": "envtraverse-mem-" + session_id + "-0", "size_bytes": 10 * 1024**2,
    }]}


class MemoryBackend(FakeBackend):
    def __init__(self, *args, frame_volume=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.frame_volume = frame_volume


def test_adapter_forwards_resources_separately_from_game_settings(monkeypatch):
    sid = uuid4().hex
    context = resources(sid)
    monkeypatch.setenv("ENVTRAVERSE_RESOURCES", json.dumps(context))
    adapter = EnvironmentManager({"session_id": sid})
    calls = []

    def call(method, **parameters):
        calls.append((method, parameters))
        return {"session_id": sid, "environment_description": "Minecraft", "observation": observation({})}

    monkeypatch.setattr(adapter.client, "call", call)
    adapter.start_environment("craft_wooden_pickaxe")
    assert calls[0][1]["resources"] == context
    assert "resources" not in calls[0][1]["settings"]


def test_adapter_rejects_resources_for_another_session(monkeypatch):
    monkeypatch.setenv("ENVTRAVERSE_RESOURCES", json.dumps(resources(uuid4().hex)))
    with pytest.raises(ValueError, match="different session"):
        EnvironmentManager({"session_id": uuid4().hex})


def test_runtime_ram_mode_updates_spectator_and_recordings_without_workspace_writes(tmp_path):
    service = RuntimeService(tmp_path, backend_factory=MemoryBackend, clock=Clock(), network_factory=FakeNetwork)
    sid, token = uuid4().hex, uuid4().hex
    try:
        # There is deliberately no agent workspace: the host no longer publishes there.
        response = service.dispatch(request("start", sid, token, game_id="craft_wooden_pickaxe",
                                            settings={}, resources=resources(sid)))
        session = service.sessions[sid]
        assert session.backend.frame_volume == resources(sid)["mem_disk_mounts"][0]["volume"]
        assert MEMORY_FRAME_PATH in response["environment_description"]
        assert WORKSPACE_FRAME_PATH not in response["environment_description"]
        assert session.publisher.fd is None
        assert session.publisher.frame()[0] == JPEG
        assert not (tmp_path / "run").exists()
        frames = list((session.directory / "frames").glob("*.jpg"))
        assert frames and frames[0].read_bytes() == JPEG
        saved = json.loads((session.directory / "session.json").read_text())
        assert saved["resources"] == resources(sid)
        assert saved["latest_frame_path"] == MEMORY_FRAME_PATH
        sequence = session.publisher.frame()[1]
        deadline = time.monotonic() + 2
        while session.publisher.frame()[1] <= sequence and time.monotonic() < deadline:
            time.sleep(0.01)
        assert session.publisher.frame()[1] > sequence
        service.dispatch(request("close", sid, token))
        assert session.closed_event.wait(3)
        assert session.backend.stopped
    finally:
        service.shutdown()


def test_unrelated_memory_mount_preserves_workspace_publication(tmp_path):
    service = RuntimeService(tmp_path, backend_factory=FakeBackend, clock=Clock(), network_factory=FakeNetwork)
    sid, token = uuid4().hex, uuid4().hex
    workspace = prepare_workspace(tmp_path, sid)
    try:
        response = service.dispatch(request("start", sid, token, game_id="craft_wooden_pickaxe",
                                            settings={}, resources=resources(sid, "/scratch")))
        assert WORKSPACE_FRAME_PATH in response["environment_description"]
        assert (workspace / "latest_frame.jpg").read_bytes() == JPEG
    finally:
        service.shutdown()


def test_runtime_rejects_other_session_volume_before_reserving_resources(tmp_path):
    service = RuntimeService(tmp_path, backend_factory=MemoryBackend, network_factory=FakeNetwork)
    sid = uuid4().hex
    try:
        with pytest.raises(RuntimeFault, match="different session") as error:
            service.dispatch(request("start", sid, uuid4().hex, game_id="craft_wooden_pickaxe",
                                     settings={}, resources=resources(uuid4().hex)))
        assert error.value.code == "bad_request"
        assert not service.sessions and not service.owners and not service.network.created
    finally:
        service.shutdown()


def test_recorder_keeps_live_rate_independent_of_archive_rate(tmp_path):
    publisher = FramePublisher(tmp_path / "no-workspace", uuid4().hex, tmp_path, 1,
                               publish_to_workspace=False)
    try:
        for sequence, now in enumerate((0, 0.03, 0.06, 1.01), start=1):
            assert publisher.publish(JPEG, sequence, now, now=now)
            assert publisher.frame() == (JPEG, sequence, now)
        assert len(list((tmp_path / "frames").glob("*.jpg"))) == 2
        assert not (tmp_path / "latest_frame.jpg").exists()
    finally:
        publisher.close()
    assert not publisher.publish(JPEG, 5, 2, now=2)


def memory_backend(tmp_path):
    sid = uuid4().hex
    return DockerBackend(sid, tmp_path, Settings(), accept_eula=True,
                         network_name="test-runtime-network", frame_volume=resources(sid)["mem_disk_mounts"][0]["volume"])


def test_compose_mounts_controller_owned_volume_only_into_graphical_client(tmp_path):
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI not installed")
    version = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, timeout=10)
    if version.returncode:
        pytest.skip("Docker Compose plugin not installed")
    backend = memory_backend(tmp_path)
    document = json.loads(backend.command("config", "--format", "json", capture=True))
    assert document["volumes"]["latest_frame"] == {"name": backend.frame_volume, "external": True}
    client = document["services"]["client"]
    assert client["environment"]["LATEST_FRAME_PATH"] == MEMORY_FRAME_PATH
    mounts = [mount for mount in client["volumes"] if mount["target"] == "/latest_frame"]
    assert len(mounts) == 1
    assert mounts[0]["source"] == "latest_frame"
    assert mounts[0]["type"] == "volume"
    assert mounts[0]["volume"]["nocopy"] is True
    assert not any(mount["target"] == "/latest_frame" for mount in document["services"]["server"]["volumes"])


def test_cleanup_uses_same_external_volume_override_as_launch(tmp_path, monkeypatch):
    backend = memory_backend(tmp_path)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    backend.command("up", "-d")
    backend.stop()
    assert backend.stopped
    assert any("down" in command for command in calls)
    for command in calls:
        assert command[:6] == ["docker", "compose", "-f", str(backend.compose),
                               "-f", str(tmp_path / "compose-memory.json")]
    document = json.loads((tmp_path / "compose-memory.json").read_text())
    assert document["volumes"]["latest_frame"]["external"] is True


def stub_start(backend, monkeypatch, health):
    monkeypatch.setattr(backend, "command", lambda *args, **kwargs:
                        "127.0.0.1:39483\n" if args[0] == "port" else "" if args[0] == "ps" else None)
    monkeypatch.setattr(backend, "check_startup_containers", lambda containers: None)
    monkeypatch.setattr(backend, "request", lambda *args, **kwargs: (json.dumps(health).encode(), {}))
    monkeypatch.setattr(backend, "read_state", lambda:
                        ({"connected": True, "server_running": True, "time_ms": time.time() * 1000}, []))
    monkeypatch.setattr(backend, "frame", lambda: (JPEG, 1, time.time()))


@pytest.mark.parametrize("extra", [{}, {"latest_frame_path": "/wrong/latest_frame.jpg", "latest_frame_ready": True}])
def test_cached_client_without_ram_publication_fails_with_rebuild_guidance(tmp_path, monkeypatch, extra):
    backend = memory_backend(tmp_path)
    stub_start(backend, monkeypatch, {"capture_ready": True, "client": {"state": "running"}, **extra})
    with pytest.raises(RuntimeError, match="--rebuild-images"):
        backend.start()


def test_ram_publication_failure_fails_startup_without_waiting_for_deadline(tmp_path, monkeypatch):
    backend = memory_backend(tmp_path)
    stub_start(backend, monkeypatch, {"capture_ready": False, "latest_frame_path": MEMORY_FRAME_PATH,
                                     "latest_frame_ready": False, "capture_error": "No space left on device"})
    with pytest.raises(RuntimeError, match="frame publication failed.*No space"):
        backend.start()


def test_ram_start_waits_until_frame_is_published(tmp_path, monkeypatch):
    backend = memory_backend(tmp_path)
    health = {"capture_ready": True, "client": {"state": "running"},
              "latest_frame_path": MEMORY_FRAME_PATH, "latest_frame_ready": False}
    stub_start(backend, monkeypatch, health)
    responses = []

    def request_health(*args, **kwargs):
        snapshot = deepcopy(health)
        snapshot["latest_frame_ready"] = len(responses) > 0
        responses.append(snapshot)
        return json.dumps(snapshot).encode(), {}

    monkeypatch.setattr(backend, "request", request_health)
    state, frame = backend.start()
    assert len(responses) == 2
    assert frame[0] == JPEG
    assert state["connected"]


def test_host_runtime_help_works_outside_repository(tmp_path):
    script = Path(__file__).resolve().parents[1] / "host_runtime/run.sh"
    result = subprocess.run(["bash", str(script), "--help"], cwd=tmp_path, text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "--run-root" in result.stdout
