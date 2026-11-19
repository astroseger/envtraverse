"""Ownership and failure recovery for the runtime's shared Docker network."""
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
from types import SimpleNamespace

import pytest

from life_minecraft import network as module
from life_minecraft.network import OWNER_LABEL, RuntimeNetwork


class Docker:
    def __init__(self):
        self.networks = {}
        self.calls = []
        self.creation_error = None
        self.active_endpoints = False
        self.daemon_error = False

    def __call__(self, command, **kwargs):
        assert command[:2] == ["docker", "network"]
        assert kwargs == {"check": False, "capture_output": True, "text": True, "timeout": 30}
        self.calls.append(command[2:])
        action = command[2]
        if self.daemon_error:
            return SimpleNamespace(returncode=1, stdout="", stderr="Cannot connect to the Docker daemon")
        if action == "create":
            name = command[-1]
            owner = command[command.index("--label") + 1].split("=", 1)[1]
            identifier = format(len(self.networks) + 1, "064x")
            self.networks[identifier] = {"Id": identifier, "Name": name,
                                         "Labels": {OWNER_LABEL: owner}}
            if self.creation_error:
                raise self.creation_error
            return SimpleNamespace(returncode=0, stdout=identifier + "\n", stderr="")
        reference = command[3]
        value = next((record for key, record in self.networks.items()
                      if reference in (key, record["Name"])), None)
        if value is None:
            return SimpleNamespace(returncode=1, stdout="[]", stderr=f"Error: No such network: {reference}")
        if action == "inspect":
            return SimpleNamespace(returncode=0, stdout=json.dumps([value]), stderr="")
        assert action == "rm"
        if self.active_endpoints:
            return SimpleNamespace(returncode=1, stdout="", stderr="network has active endpoints")
        del self.networks[value["Id"]]
        return SimpleNamespace(returncode=0, stdout=reference, stderr="")


@pytest.fixture
def docker(monkeypatch):
    value = Docker()
    monkeypatch.setattr(module.subprocess, "run", value)
    return value


def test_creation_and_close_are_idempotent_and_thread_safe(tmp_path, docker):
    owner = RuntimeNetwork(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(lambda _: owner.create(), range(8))) == [owner.name] * 8
    assert sum(call[0] == "create" for call in docker.calls) == 1
    saved = json.loads((tmp_path / "network.json").read_text())
    assert saved["id"] == owner.network_id and saved["state"] == "ready"
    identifier = owner.network_id
    owner.close()
    owner.close()
    assert docker.calls[-1] == ["rm", identifier]
    assert sum(call[0] == "rm" for call in docker.calls) == 1
    assert not docker.networks
    assert owner.metadata()["state"] == "closed"
    assert owner.metadata()["cleanup_required"] is False


def test_runtime_names_and_ownership_are_unique(tmp_path, docker):
    first_dir, second_dir = tmp_path / "one", tmp_path / "two"
    first_dir.mkdir(); second_dir.mkdir()
    first, second = RuntimeNetwork(first_dir), RuntimeNetwork(second_dir)
    first.create(); second.create()
    assert first.name != second.name and first.owner_id != second.owner_id
    first.close()
    assert second.network_id in docker.networks
    second.close()


def test_close_without_creation_does_not_call_docker(tmp_path, docker):
    owner = RuntimeNetwork(tmp_path)
    owner.close()
    assert docker.calls == []
    with pytest.raises(RuntimeError, match="already been closed"):
        owner.create()


def test_missing_owned_id_does_not_remove_replacement_with_same_name(tmp_path, docker):
    owner = RuntimeNetwork(tmp_path)
    owner.create()
    previous = docker.networks.pop(owner.network_id)
    foreign_id = "f" * 64
    docker.networks[foreign_id] = {**previous, "Id": foreign_id, "Labels": {OWNER_LABEL: "someone-else"}}
    owner.close()
    assert foreign_id in docker.networks
    assert not any(call[0] == "rm" for call in docker.calls)


def test_foreign_ownership_is_never_removed(tmp_path, docker):
    owner = RuntimeNetwork(tmp_path)
    owner.create()
    docker.networks[owner.network_id]["Labels"][OWNER_LABEL] = "another-runtime"
    with pytest.raises(RuntimeError, match="ownership does not match"):
        owner.close()
    assert not any(call[0] == "rm" for call in docker.calls)
    assert owner.metadata()["cleanup_required"]
    assert "ownership" in owner.cleanup_error


def test_active_endpoints_retain_network_and_report_cleanup_failure(tmp_path, docker):
    owner = RuntimeNetwork(tmp_path)
    owner.create()
    docker.active_endpoints = True
    with pytest.raises(RuntimeError, match="active endpoints"):
        owner.close()
    assert owner.network_id in docker.networks
    saved = json.loads((tmp_path / "network.json").read_text())
    assert saved["state"] == "cleanup_failed" and saved["cleanup_required"]
    assert all(call[0] != "disconnect" for call in docker.calls)
    docker.active_endpoints = False
    owner.close()
    assert not owner.cleanup_error and not docker.networks


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("docker network create", 30), KeyboardInterrupt()])
def test_partial_creation_recovers_exact_owned_name_for_cleanup(tmp_path, docker, error):
    owner = RuntimeNetwork(tmp_path)
    docker.creation_error = error
    with pytest.raises(type(error)):
        owner.create()
    assert owner.network_id is None and len(docker.networks) == 1
    owner.close()
    assert docker.calls[1] == ["inspect", owner.name]
    assert docker.calls[2][0] == "rm" and len(docker.calls[2][1]) == 64
    assert not docker.networks


def test_failed_ready_metadata_write_does_not_lose_network_identity(tmp_path, docker, monkeypatch):
    owner = RuntimeNetwork(tmp_path)
    real_write = module.atomic_json
    def write(path, value):
        if value["state"] == "ready":
            raise OSError("disk full")
        real_write(path, value)
    monkeypatch.setattr(module, "atomic_json", write)
    with pytest.raises(OSError, match="disk full"):
        owner.create()
    identifier = owner.network_id
    assert identifier in docker.networks
    owner.close()
    assert docker.calls[-1] == ["rm", identifier]
    assert not docker.networks


def test_unavailable_daemon_is_not_mistaken_for_absent_network(tmp_path, docker):
    owner = RuntimeNetwork(tmp_path)
    owner.create()
    docker.daemon_error = True
    with pytest.raises(RuntimeError, match="Cannot connect"):
        owner.close()
    assert owner.metadata()["state"] == "cleanup_failed"
    assert owner.network_id in docker.networks
