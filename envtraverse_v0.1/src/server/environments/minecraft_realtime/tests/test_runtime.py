"""Runtime integration tests use real sockets/files and a fake game backend."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest

from minecraft_realtime.catalog import list_tasks
from minecraft_realtime.docker_backend import DockerBackend
from minecraft_realtime.protocol import PROTOCOL_VERSION, RuntimeClient, RuntimeFault, decode_observation
from minecraft_realtime.recording import FramePublisher, JsonlTail
from minecraft_realtime.runtime import RuntimeServer, RuntimeService, SpectatorServer
from minecraft_realtime.settings import Settings


def test_failed_client_is_detected_without_a_running_bridge(tmp_path, monkeypatch):
    from types import SimpleNamespace
    backend = DockerBackend(uuid4().hex, tmp_path, Settings(), accept_eula=True)
    calls = []
    def inspect(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout='{"Status":"running","ExitCode":0}\n'
                               '{"Status":"exited","ExitCode":1}\n')
    monkeypatch.setattr(subprocess, "run", inspect)
    with pytest.raises(RuntimeError, match="client failed.*exit code 1") as failure:
        backend.check_startup_containers([
            {"service": "server", "container_id": "server-id"},
            {"service": "client", "container_id": "client-id"}])
    assert str(tmp_path / "client-logs/launcher.log") in str(failure.value)
    assert calls == [["docker", "inspect", "--format", "{{json .State}}", "server-id", "client-id"]]


def test_running_containers_are_not_mistaken_for_startup_failure(tmp_path, monkeypatch):
    from types import SimpleNamespace
    backend = DockerBackend(uuid4().hex, tmp_path, Settings(), accept_eula=True)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs:
        SimpleNamespace(stdout='{"Status":"running","ExitCode":0}\n'))
    backend.check_startup_containers([{"service": "client", "container_id": "client-id"}])

JPEG = b"\xff\xd8fake-complete-test-jpeg\xff\xd9"


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


class FakeBackend:
    def __init__(self, session_id, directory, settings, *, accept_eula=False, network_name=None):
        self.session_id, self.settings = session_id, settings
        self.network_name = network_name
        self.actions, self.releases, self.stopped, self.sequence = [], 0, False, 0
        self.cancelled = threading.Event()
        self.state = {"session_id": session_id, "connected": True, "server_running": True,
                      "last_event_seq": 0, "counters": {"crafted": {}, "kills": {}, "disconnects": 0},
                      "player": {"inventory": [], "dimension": "minecraft:overworld"}}

    def start(self):
        return self.read_state()[0], self.frame()

    def frame(self):
        self.sequence += 1
        return JPEG, self.sequence, time.time()

    def read_state(self):
        state = deepcopy(self.state)
        state["time_ms"] = time.time() * 1000
        return state, []

    def read_input_events(self):
        return []

    def action(self, action):
        self.actions.append(action)
        return {"accepted": True, "action_id": len(self.actions)}

    def release(self):
        self.releases += 1

    def stop(self):
        self.stopped = True


class FakeNetwork:
    def __init__(self, directory):
        self.name = "test-runtime-network-" + uuid4().hex
        self.created = self.closed = False
        self.creations = 0

    def create(self):
        if not self.created:
            self.creations += 1
            self.created = True
        return self.name

    def close(self):
        self.closed = True

    def metadata(self):
        return {"name": self.name, "created": self.created, "closed": self.closed}


def request(method, session_id, token, **kwargs):
    return {"version": PROTOCOL_VERSION, "method": method, "session_id": session_id,
            "owner_token": token, **kwargs}


def prepare_workspace(root, session_id):
    path = root / "run" / session_id / "run" / "agent_run"
    path.mkdir(parents=True)
    return path


def start(service, *, timeout=10):
    session_id, token = uuid4().hex, uuid4().hex
    workspace = prepare_workspace(service.run_root, session_id)
    response = service.dispatch(request("start", session_id, token, game_id=list_tasks()[0]["id"],
                                        settings={"minecraft_task_timeout_seconds": timeout}))
    return session_id, token, workspace, response


@pytest.fixture
def service(tmp_path):
    value = RuntimeService(tmp_path, backend_factory=FakeBackend, clock=Clock(),
                           network_factory=FakeNetwork)
    yield value
    value.shutdown()


def test_timeout_is_recorded_while_idle_and_next_action_is_terminal(service):
    sid, token, workspace, response = start(service)
    session = service.sessions[sid]
    assert decode_observation(response["observation"])[1] is False
    action = {"id": "key_down", "key": "w"}
    acknowledgment = service.dispatch(request("action", sid, token, action=action))
    assert json.loads(decode_observation(acknowledgment["observation"])[0]["action.json"])["accepted"]
    before = session.publisher.frame()[1]
    service.clock.value += 11
    session.poll()
    assert session.result["reason"] == "minecraft_task_timeout"
    assert session.result["status"] == "failed"
    assert session.backend.releases == 0
    assert session.backend.stopped is False
    # Capture continues after the latched deadline, without another action.
    deadline = time.monotonic() + 2
    while session.publisher.frame()[1] <= before and time.monotonic() < deadline:
        time.sleep(0.01)
    assert session.publisher.frame()[1] > before
    assert (workspace / "latest_frame.jpg").read_bytes() == JPEG
    session.backend.state["counters"]["crafted"]["minecraft:wooden_pickaxe"] = 1
    session.poll()
    assert session.result["status"] == "failed"
    terminal = service.dispatch(request("action", sid, token, action={"id": "key_up", "key": "w"}))
    files, ended = decode_observation(terminal["observation"])
    assert ended and json.loads(files["action.json"])["accepted"] is False
    assert session.backend.actions == [action]
    assert session.backend.releases == 1
    events = [json.loads(line) for line in (session.directory / "events.jsonl").read_text().splitlines()]
    assert sum(event["type"] == "timeout" for event in events) == 1
    saved = (session.directory / "result.json").read_bytes()
    service.dispatch(request("close", sid, token))
    assert session.closed_event.wait(3)
    assert (session.directory / "result.json").read_bytes() == saved


def test_goal_success_is_latched_and_independent_sessions_do_not_share_state(service):
    first, token, *_ = start(service)
    second, second_token, *_ = start(service)
    one, two = service.sessions[first], service.sessions[second]
    one.backend.state["counters"]["crafted"]["minecraft:wooden_pickaxe"] = 1
    one.poll()
    assert one.result["success"] is True
    verdict = json.loads((one.directory / "verdict_state.json").read_text())
    assert verdict["state"]["counters"]["crafted"]["minecraft:wooden_pickaxe"] == 1
    assert verdict["state_age_seconds"] < 1
    assert two.result is None
    response = service.dispatch(request("action", second, second_token, action={"id": "key_down", "key": "w"}))
    assert not decode_observation(response["observation"])[1]
    assert len(two.backend.actions) == 1 and not one.backend.actions
    service.clock.value += 99
    one.poll()
    assert one.result["success"] is True
    with pytest.raises(RuntimeFault, match="another manager"):
        service.dispatch(request("close", second, uuid4().hex))
    assert not two.closed
    with pytest.raises(RuntimeFault, match="already been started"):
        service.dispatch(request("start", second, second_token, game_id=list_tasks()[0]["id"], settings={}))


def test_multiple_sessions_start_concurrently_by_default(service):
    barrier = threading.Barrier(4)

    class ConcurrentBackend(FakeBackend):
        def start(self):
            # Every backend must enter startup before any can finish it.
            barrier.wait(timeout=3)
            return super().start()

    service.backend_factory = ConcurrentBackend
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: start(service), range(4)))
    assert len({result[0] for result in results}) == 4
    assert all(session.started and not session.closed for session in service.sessions.values())
    assert service.network.creations == 1


def test_next_session_starts_while_acknowledged_cleanup_continues(service):
    sid, token, *_ = start(service)
    session = service.sessions[sid]
    entered, finish = threading.Event(), threading.Event()
    stop = session.backend.stop

    def slow_stop():
        entered.set()
        finish.wait(5)
        stop()

    session.backend.stop = slow_stop
    service.dispatch(request("close", sid, token))
    assert entered.wait(1)
    results, errors = [], []

    def next_start():
        try:
            results.append(start(service))
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=next_start)
    thread.start()
    try:
        thread.join(1)
        assert not thread.is_alive()
        assert not errors
        assert len(results) == 1
        assert not session.closed
        assert not service.sessions[results[0][0]].closed
    finally:
        finish.set()
        thread.join(3)
    assert session.closed_event.wait(3)
    assert session.backend.stopped


def test_failed_cleanup_is_recorded_without_blocking_other_sessions(service):
    sid, token, *_ = start(service)
    session = service.sessions[sid]

    def fail_stop():
        raise RuntimeError("Docker daemon unavailable")

    session.backend.stop = fail_stop
    service.dispatch(request("close", sid, token))
    assert session.closed_event.wait(3)
    assert session.cleanup_failed
    next_sid, *_ = start(service)
    assert service.sessions[next_sid].started
    assert not service.sessions[next_sid].cleanup_failed
    assert session.cleanup_failed
    metadata = json.loads((session.directory / "session.json").read_text())
    assert metadata["cleanup_error"] == "RuntimeError"
    events = [json.loads(line) for line in (session.directory / "events.jsonl").read_text().splitlines()]
    assert any(event["type"] == "session_closed" and event["data"]["cleanup_error"] == "RuntimeError"
               for event in events)


def test_close_during_backend_startup_cancels_and_cleans_session(service):
    entered = threading.Event()

    class SlowBackend(FakeBackend):
        def start(self):
            entered.set()
            if not self.cancelled.wait(3):
                raise RuntimeError("Test startup cancellation timed out")
            raise RuntimeError("Minecraft startup was cancelled")

    service.backend_factory = SlowBackend
    sid, token = uuid4().hex, uuid4().hex
    prepare_workspace(service.run_root, sid)
    errors = []

    def slow_start():
        try:
            service.dispatch(request("start", sid, token, game_id=list_tasks()[0]["id"], settings={}))
        except RuntimeFault as error:
            errors.append(error.code)

    thread = threading.Thread(target=slow_start)
    thread.start()
    try:
        assert entered.wait(1)
        with pytest.raises(RuntimeFault) as wrong_owner:
            service.dispatch(request("close", sid, uuid4().hex))
        assert wrong_owner.value.code == "not_owner"
        session = service.sessions[sid]
        assert not session.backend.cancelled.is_set()
        assert service.dispatch(request("close", sid, token)) == {"closed": True}
        thread.join(3)
        assert not thread.is_alive()
        assert errors == ["start_failed"]
        assert session.closed_event.wait(3)
        assert session.backend.cancelled.is_set()
        assert session.backend.stopped
        assert not session.cleanup_failed
        assert json.loads((session.directory / "result.json").read_text())["reason"] == "startup_failed"
        assert service.dispatch(request("close", sid, token)) == {"closed": True}
    finally:
        if sid in service.sessions:
            service.close_session(service.sessions[sid])
        thread.join(3)


def test_unknown_close_is_a_tombstone_for_only_that_owner(service):
    sid, token, other_owner = uuid4().hex, uuid4().hex, uuid4().hex
    prepare_workspace(service.run_root, sid)
    service.dispatch(request("close", sid, token))
    with pytest.raises(RuntimeFault) as cancelled:
        service.dispatch(request("start", sid, token, game_id=list_tasks()[0]["id"], settings={}))
    assert cancelled.value.code == "cancelled"
    # Unknown close has no authority to cancel another owner's later start.
    response = service.dispatch(request("start", sid, other_owner, game_id=list_tasks()[0]["id"], settings={}))
    assert response["session_id"] == sid
    with pytest.raises(RuntimeFault) as refused:
        service.dispatch(request("close", sid, token))
    assert refused.value.code == "not_owner"


def test_startup_failure_records_error_and_releases_owned_backend(service):
    class BrokenBackend(FakeBackend):
        def start(self):
            raise RuntimeError("private Docker details")
    service.backend_factory = BrokenBackend
    sid, token = uuid4().hex, uuid4().hex
    prepare_workspace(service.run_root, sid)
    with pytest.raises(RuntimeFault, match="inspect host run_mc logs") as error:
        service.dispatch(request("start", sid, token, game_id=list_tasks()[0]["id"], settings={}))
    assert "private Docker details" not in str(error.value)
    session = service.sessions[sid]
    assert session.closed_event.wait(3)
    assert session.backend.stopped
    assert json.loads((session.directory / "result.json").read_text())["reason"] == "startup_failed"


def test_frame_publication_replaces_symlink_without_following_and_anchors_workspace(tmp_path):
    sid = uuid4().hex
    workspace = prepare_workspace(tmp_path, sid)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("do not change")
    (workspace / "latest_frame.jpg").symlink_to(outside)
    publisher = FramePublisher(tmp_path, sid, artifacts, 1)
    previous_umask = os.umask(0o077)
    try:
        assert publisher.publish(JPEG, 1, 123, now=0)
        assert outside.read_text() == "do not change"
        assert not (workspace / "latest_frame.jpg").is_symlink()
        assert (workspace / "latest_frame.jpg").stat().st_mode & 0o777 == 0o644
        assert not publisher.publish(JPEG, 1, 123, now=0.1)
        publisher.publish(JPEG, 2, 124, now=0.1)
        publisher.publish(JPEG, 3, 125, now=1.1)
        assert len(list((artifacts / "frames").glob("*.jpg"))) == 2
        moved = workspace.with_name("old_workspace")
        workspace.rename(moved)
        workspace.symlink_to(tmp_path, target_is_directory=True)
        publisher.publish(JPEG, 4, 126, now=2.1)
        assert (moved / "latest_frame.jpg").read_bytes() == JPEG
        assert not (tmp_path / "latest_frame.jpg").exists()
    finally:
        os.umask(previous_umask)
        publisher.close()


def test_frame_publication_rejects_symlink_workspace(tmp_path):
    sid = uuid4().hex
    workspace = prepare_workspace(tmp_path, sid)
    workspace.rmdir()
    workspace.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        FramePublisher(tmp_path, sid, tmp_path, 1)


def test_jsonl_tail_waits_for_complete_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    reader = JsonlTail(path)
    assert reader.read() == []
    path.write_text('{"seq":1}\n{"seq":')
    assert reader.read() == [{"seq": 1}]
    with path.open("a") as out:
        out.write('2}\n')
    assert reader.read() == [{"seq": 2}]
    assert reader.read() == []


def test_unix_protocol_and_spectator_are_independent(service, tmp_path):
    socket_path = tmp_path / "runtime.sock"
    unix = RuntimeServer(socket_path, service)
    viewer = SpectatorServer(("127.0.0.1", 0), service)
    for server in (unix, viewer):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        sid, token = uuid4().hex, uuid4().hex
        prepare_workspace(service.run_root, sid)
        client = RuntimeClient(socket_path, timeout=3)
        client.call("start", session_id=sid, owner_token=token, game_id=list_tasks()[0]["id"], settings={})
        base = f"http://127.0.0.1:{viewer.server_port}"
        with urlopen(base + "/") as response:
            assert f"/sessions/{sid}/view".encode() in response.read()
        with urlopen(base + "/sessions.json") as response:
            assert json.loads(response.read())["sessions"][0]["session_id"] == sid
        with urlopen(base + f"/sessions/{sid}/frame.jpg") as response:
            assert response.read() == JPEG
        with urlopen(base + f"/sessions/{sid}/view") as response:
            assert b"Read-only" in response.read()
        with pytest.raises(HTTPError) as error:
            urlopen(Request(base + f"/sessions/{sid}/input", data=b"{}"))
        assert error.value.code == 405
        assert not service.sessions[sid].backend.actions
        client.call("close", session_id=sid, owner_token=token)
        assert service.sessions[sid].closed_event.wait(3)
    finally:
        for server in (unix, viewer):
            server.shutdown()
            server.server_close()


def test_startup_cancellation_reaps_compose_process_before_cleanup(tmp_path, monkeypatch):
    backend = DockerBackend(uuid4().hex, tmp_path, Settings(), accept_eula=True)
    real_popen = subprocess.Popen
    children, errors = [], []
    launched = threading.Event()
    def start_long_command(*args, **kwargs):
        child = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        launched.set()
        return child
    monkeypatch.setattr(subprocess, "Popen", start_long_command)
    def startup():
        try:
            backend.command("up", cancellable=True, timeout=30)
        except RuntimeError as error:
            errors.append(str(error))
    thread = threading.Thread(target=startup)
    thread.start()
    assert launched.wait(2)
    backend.cancelled.set()
    thread.join(3)
    assert not thread.is_alive()
    assert errors == ["Minecraft startup was cancelled"]
    assert children[0].poll() is not None
