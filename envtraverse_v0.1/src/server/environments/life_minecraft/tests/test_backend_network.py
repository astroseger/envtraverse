"""Shared-network routing and per-session backend configuration."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from life_minecraft.docker_backend import DockerBackend
from life_minecraft.settings import Settings


def backend(tmp_path, network_name="test-runtime-network"):
    directory = tmp_path / uuid4().hex
    directory.mkdir()
    return DockerBackend(uuid4().hex, directory, Settings(), accept_eula=True,
                         network_name=network_name)


def test_sessions_share_only_the_network_and_keep_unique_server_addresses(tmp_path, monkeypatch):
    monkeypatch.setenv("MC_NETWORK", "unrelated-network")
    monkeypatch.setenv("MC_SERVER_HOST", "server")
    first, second = backend(tmp_path), backend(tmp_path)
    assert first.environment["MC_NETWORK"] == second.environment["MC_NETWORK"] == "test-runtime-network"
    assert first.environment["MC_SERVER_HOST"] == first.server_host
    assert second.environment["MC_SERVER_HOST"] == second.server_host
    assert first.server_host != second.server_host
    assert first.project != second.project
    assert os.environ["MC_NETWORK"] == "unrelated-network"
    assert os.environ["MC_SERVER_HOST"] == "server"


@pytest.mark.parametrize("network_name", [None, "", "   "])
def test_start_requires_runtime_network_before_launching_compose(tmp_path, monkeypatch, network_name):
    monkeypatch.setenv("MC_NETWORK", "inherited-network-must-not-be-used")
    game = backend(tmp_path, network_name)
    monkeypatch.setattr(game, "command", lambda *a, **kw: pytest.fail("unexpected Compose invocation"))
    with pytest.raises(RuntimeError, match="runtime must provide a shared Docker network"):
        game.start()
    if network_name is None:
        assert "MC_NETWORK" not in game.environment


def test_started_backend_records_network_and_its_specific_server(tmp_path, monkeypatch):
    game = backend(tmp_path)
    monkeypatch.setattr(game, "command", lambda *a, **kw:
                        "127.0.0.1:39483\n" if a[0] == "port" else "server-id\nclient-id" if a[0] == "ps" else None)
    containers = [
        {"Id": service + "-id", "Image": "sha256:" + service,
         "Config": {"Labels": {"com.docker.compose.service": service}}}
        for service in ("server", "client")
    ]
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=json.dumps(containers)))
    monkeypatch.setattr(game, "check_startup_containers", lambda images: None)
    monkeypatch.setattr(game, "request", lambda *a, **kw:
                        (b'{"capture_ready":true,"client":{"state":"running"}}', {}))
    monkeypatch.setattr(game, "read_state", lambda:
                        ({"connected": True, "server_running": True, "time_ms": time.time() * 1000}, []))
    monkeypatch.setattr(game, "frame", lambda: (b"jpeg", 1, time.time()))
    game.start()
    metadata = json.loads((game.directory / "backend.json").read_text())
    assert metadata["network"] == "test-runtime-network"
    assert metadata["server_host"] == game.server_host
    assert metadata["client_url"] == "http://127.0.0.1:39483"


def test_compose_renders_one_external_network_and_correct_client_destination(tmp_path):
    """Compose config is local validation: no daemon, containers, or networks."""
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI not installed")
    version = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, timeout=10)
    if version.returncode:
        pytest.skip("Docker Compose plugin not installed")
    first, second = backend(tmp_path), backend(tmp_path)
    for game in (first, second):
        document = json.loads(game.command("config", "--format", "json", capture=True))
        assert set(document["networks"]) == {"shared"}
        assert document["networks"]["shared"]["name"] == "test-runtime-network"
        assert document["networks"]["shared"]["external"] is True
        assert not document["networks"]["shared"].get("ipam", {}).get("config")
        server = document["services"]["server"]
        client = document["services"]["client"]
        assert set(server["networks"]) == set(client["networks"]) == {"shared"}
        assert game.server_host in server["networks"]["shared"]["aliases"]
        assert client["environment"]["SERVER_HOST"] == game.server_host
        assert client["environment"]["SERVER_PORT"] == "25565"
        assert not server.get("ports")
        assert len(client["ports"]) == 1
        assert client["ports"][0]["host_ip"] == "127.0.0.1"
        assert client["ports"][0]["target"] == 8000
        assert client["ports"][0].get("published") in (None, "", "0", 0)
        assert {volume["source"] for volume in server["volumes"] if volume["type"] == "volume"} == {"world"}
        assert {volume["source"] for volume in client["volumes"] if volume["type"] == "volume"} == {"client"}
