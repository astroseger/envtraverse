"""Shared network ownership must outlive individual Minecraft sessions."""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from life_minecraft.protocol import RuntimeFault
from life_minecraft.runtime import RuntimeService
from life_minecraft.tests.test_runtime import Clock, FakeBackend, FakeNetwork, request, start


def service_at(path):
    return RuntimeService(path, backend_factory=FakeBackend, network_factory=FakeNetwork,
                          clock=Clock())


def test_concurrent_games_share_one_network_and_closing_one_keeps_other_alive(tmp_path):
    service = service_at(tmp_path)
    try:
        service.prepare()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(start, service) for _ in range(2)]
            first, second = [future.result() for future in futures]
        assert service.network.creations == 1
        first_id, first_token, *_ = first
        second_id, second_token, second_workspace, _ = second
        one, two = service.sessions[first_id], service.sessions[second_id]
        assert one.backend.network_name == two.backend.network_name == service.network.name
        service.dispatch(request("close", first_id, first_token))
        assert one.closed_event.wait(3)
        assert one.backend.stopped
        assert not service.network.closed
        assert not two.closed and not two.backend.stopped
        service.dispatch(request("action", second_id, second_token,
                                 action={"id": "key_press", "key": "w", "duration_ms": 250}))
        assert len(two.backend.actions) == 1
        assert not one.backend.actions
        assert (second_workspace / "latest_frame.jpg").is_file()
    finally:
        service.shutdown()
    assert service.network.closed
    assert two.backend.stopped


def test_network_is_removed_only_after_all_session_cleanup_finishes(tmp_path):
    service = service_at(tmp_path)
    start(service)
    start(service)
    close = service.network.close
    checked = []

    def checked_close():
        assert all(session.closed and session.backend.stopped for session in service.sessions.values())
        checked.append(True)
        close()

    service.network.close = checked_close
    service.shutdown()
    assert checked == [True]
    assert service.network_cleanup_error is None


def test_network_cleanup_failure_is_retained_for_runtime_exit_status(tmp_path):
    service = service_at(tmp_path)
    service.prepare()

    def failed_close():
        raise RuntimeError("network still has active endpoints")

    service.network.close = failed_close
    service.shutdown()
    assert service.network_cleanup_error == "network still has active endpoints"
    assert not service.network.closed


def test_failed_network_creation_never_starts_a_game(tmp_path):
    service = service_at(tmp_path)

    def failed_create():
        raise RuntimeError("address pool exhausted")

    service.network.create = failed_create
    try:
        with pytest.raises(RuntimeFault, match="workspace preparation failed"):
            start(service)
        assert not service.sessions
        results = list((tmp_path / "run_mc/sessions").glob("*/result.json"))
        assert len(results) == 1
        assert json.loads(results[0].read_text())["reason"] == "preparation_failed"
    finally:
        service.shutdown()
    assert service.network.closed


def test_separate_runtimes_own_separate_networks(tmp_path):
    first, second = service_at(tmp_path / "first"), service_at(tmp_path / "second")
    try:
        start(first)
        start(second)
        assert first.network.name != second.network.name
        first.shutdown()
        assert first.network.closed
        assert not second.network.closed
        assert all(not session.backend.stopped for session in second.sessions.values())
    finally:
        first.shutdown()
        second.shutdown()
