from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.controller import run_funs as rf
import run_controller_mc as mc


def test_session_id_is_assigned_and_cannot_be_overridden(tmp_path):
    paths = rf.RunPaths(tmp_path)
    assert paths.run_mc_dir == tmp_path / "run_mc"
    environment = rf.EnvironmentConfig("minecraft_realtime", {"minecraft_task_timeout_seconds": 10}, [])
    command = rf.docker_server_command("server", "internet", "internal", environment,
                                      "f" * 32, "craft_wooden_pickaxe", image="server:example", paths=paths)
    parameters = json.loads(command[command.index("--parameters") + 1])
    assert parameters == {"minecraft_task_timeout_seconds": 10, "session_id": "f" * 32}
    with pytest.raises(RuntimeError, match="session_id"):
        rf.parse_environment_config({"environment": "minecraft_realtime",
            "environment_parameters": {"session_id": "a" * 32}}, tmp_path / "config.yaml", paths=paths)


def test_wrapper_refuses_existing_run_without_starting_any_process(tmp_path, monkeypatch):
    (tmp_path / "run_mc").mkdir()
    monkeypatch.setattr(mc, "start_process", lambda *a, **k: pytest.fail("unexpected process"))
    with pytest.raises(RuntimeError, match="already exists"):
        mc.run_experiment(ROOT, tmp_path / "config.yaml", paths=rf.RunPaths(tmp_path), accept_eula=True)


def test_wrapper_requires_eula_before_starting_any_process(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("environment: minecraft_realtime\n")
    monkeypatch.delenv("EULA", raising=False)
    monkeypatch.setattr(mc, "start_process", lambda *a, **k: pytest.fail("unexpected process"))
    with pytest.raises(RuntimeError, match="EULA"):
        mc.run_experiment(ROOT, config, paths=rf.RunPaths(tmp_path))


def test_wrapper_drains_runtime_after_controller(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("environment: minecraft_realtime\n")
    calls = []
    class Process:
        stdout = iter(())
        def __init__(self, label): self.label = label
    class Thread:
        def __init__(self, **kwargs): pass
        def start(self): pass
        def join(self, **kwargs): pass
    def start(root, command, **kwargs):
        calls.append(("start", command))
        return Process("runtime" if command[1].endswith("run.sh") else "setup")
    monkeypatch.setattr(mc, "start_process", start)
    monkeypatch.setattr(mc, "needs_image_build", lambda **kw: True)
    monkeypatch.setattr(mc, "pin_images", lambda env: {})
    monkeypatch.setattr(mc.subprocess, "Popen", lambda *a, **kw: Process("controller"))
    monkeypatch.setattr(mc, "wait_process", lambda *a, **kw: 0)
    monkeypatch.setattr(mc, "wait_ready", lambda *a, **kw: {"status": "ready"})
    monkeypatch.setattr(mc.threading, "Thread", Thread)
    monkeypatch.setattr(mc, "stop_process", lambda p, **kw: calls.append(("stop", p.label)) if p else None)
    assert mc.run_experiment(ROOT, config, paths=rf.RunPaths(tmp_path), accept_eula=True) == 0
    assert [label for kind, label in calls if kind == "stop"] == ["setup", "controller", "runtime"]
    runtime_command = next(command for kind, command in calls
                           if kind == "start" and command[1].endswith("run.sh"))
    assert "--max-sessions" not in runtime_command


def test_prepared_images_are_reused_by_default(monkeypatch):
    monkeypatch.delenv("MC_SERVER_IMAGE", raising=False)
    monkeypatch.delenv("MC_CLIENT_IMAGE", raising=False)
    monkeypatch.setattr(mc, "inspect_images", lambda: {
        "server": {"prepared": True}, "client": {"prepared": True}})
    assert mc.needs_image_build() is False
    assert mc.needs_image_build(skip=True) is False
    assert mc.needs_image_build(rebuild=True) is True


def test_missing_or_old_images_are_prepared_once(monkeypatch):
    monkeypatch.delenv("MC_SERVER_IMAGE", raising=False)
    monkeypatch.delenv("MC_CLIENT_IMAGE", raising=False)
    monkeypatch.setattr(mc, "inspect_images", lambda: {
        "server": {"prepared": True}, "client": {"prepared": False}})
    assert mc.needs_image_build() is True
    with pytest.raises(RuntimeError, match="prepared assets"):
        mc.needs_image_build(skip=True)
    monkeypatch.setattr(mc, "inspect_images", lambda: {"server": None, "client": None})
    assert mc.needs_image_build() is True
