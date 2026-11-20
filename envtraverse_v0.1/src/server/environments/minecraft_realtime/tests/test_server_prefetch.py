from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "game/server/prefetch.py"
SPEC = importlib.util.spec_from_file_location("server_prefetch", SOURCE)
prefetch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prefetch)


def write_runtime(directory):
    for name in (".fabric/server/1.21.1-server.jar",
                 ".fabric/server/fabric-loader-server-0.16.10-minecraft-1.21.1.jar",
                 "versions/1.21.1/server-1.21.1.jar",
                 "libraries/net/fabricmc/fabric-loader/0.16.10/fabric-loader-0.16.10.jar"):
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as output:
            output.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")


def test_prefetch_preserves_only_valid_runtime_with_eula_unaccepted(tmp_path):
    def run(command, **kwargs):
        assert (tmp_path / "eula.txt").read_text() == "eula=false\n"
        assert kwargs["cwd"] == tmp_path
        assert "nogui" in command
        write_runtime(tmp_path)
        (tmp_path / "logs").mkdir()
        (tmp_path / "logs/latest.log").write_text("bootstrap output")
        (tmp_path / "mods").mkdir()
        return SimpleNamespace(returncode=0, stdout=prefetch.EULA_REFUSAL)
    prefetch.prepare(tmp_path, "1.21.1", "0.16.10", run=run)
    assert {path.name for path in tmp_path.iterdir()} == prefetch.CACHE_DIRECTORIES
    assert not (tmp_path / "eula.txt").exists()


def test_prefetch_retries_download_failure_and_removes_partial_jar(tmp_path):
    calls = []
    partial = tmp_path / "libraries/incomplete.jar"
    def run(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            partial.parent.mkdir()
            partial.write_bytes(b"partial download")
            return SimpleNamespace(returncode=1, stdout="Network is unreachable")
        assert not partial.exists()
        write_runtime(tmp_path)
        return SimpleNamespace(returncode=0, stdout=prefetch.EULA_REFUSAL)
    prefetch.prepare(tmp_path, "1.21.1", "0.16.10", run=run, sleep=lambda _: None)
    assert len(calls) == 2


def test_prefetch_does_not_retry_or_accept_a_world(tmp_path):
    calls = []
    def run(*args, **kwargs):
        calls.append(1)
        (tmp_path / "world").mkdir()
        return SimpleNamespace(returncode=0, stdout=prefetch.EULA_REFUSAL)
    with pytest.raises(RuntimeError, match="created a world"):
        prefetch.prepare(tmp_path, "1.21.1", "0.16.10", run=run, sleep=lambda _: None)
    assert len(calls) == 1


def test_prefetch_refuses_existing_world_without_invoking_java(tmp_path):
    (tmp_path / "world").mkdir()
    with pytest.raises(RuntimeError, match="existing game world"):
        prefetch.prepare(tmp_path, "1.21.1", "0.16.10", run=lambda *a, **k: pytest.fail("Unexpected Java launch"))


def test_prefetch_requires_complete_jars_even_after_clean_exit(tmp_path):
    calls = []
    def run(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(returncode=0, stdout=prefetch.EULA_REFUSAL)
    with pytest.raises(RuntimeError, match="required Minecraft/Fabric"):
        prefetch.prepare(tmp_path, "1.21.1", "0.16.10", run=run, sleep=lambda _: None)
    assert len(calls) == 3
