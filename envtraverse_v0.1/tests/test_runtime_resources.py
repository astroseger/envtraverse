import json
from uuid import uuid4

import pytest

from src.server.runtime_resources import parse_resource_context, read_resource_context


def context():
    session = uuid4().hex
    return {"session_id": session, "mem_disk_mounts": [
        {"mount": "/latest_frame", "volume": f"envtraverse-mem-{session}-0", "size_bytes": 10485760}]}


def test_resource_context_round_trip(monkeypatch):
    value = context()
    monkeypatch.setenv("ENVTRAVERSE_RESOURCES", json.dumps(value))
    parsed = read_resource_context(session_id=value["session_id"])
    assert parsed == value
    parsed["mem_disk_mounts"][0]["mount"] = "/changed"
    assert value["mem_disk_mounts"][0]["mount"] == "/latest_frame"


def test_absent_context_is_optional(monkeypatch):
    monkeypatch.delenv("ENVTRAVERSE_RESOURCES", raising=False)
    assert read_resource_context() == parse_resource_context(None) == parse_resource_context({}) == {}


def test_resources_cannot_cross_sessions():
    value = context()
    with pytest.raises(ValueError, match="different session"):
        parse_resource_context(value, session_id=uuid4().hex)
    value["mem_disk_mounts"][0]["volume"] = f"envtraverse-mem-{uuid4().hex}-0"
    with pytest.raises(ValueError, match="does not belong"):
        parse_resource_context(value)


@pytest.mark.parametrize("field,value", [
    ("mount", "/"), ("mount", "relative"), ("mount", "//frame"),
    ("mount", "/frame/../data"), ("mount", "/frame/"), ("mount", "/frame,ro"),
    ("mount", "/frame:ro"), ("mount", "/frame\n"),
    ("size_bytes", 0), ("size_bytes", True), ("size_bytes", 1.5),
    ("size_bytes", 2**63), ("volume", "some-other-volume"),
])
def test_malformed_entries_rejected(field, value):
    resource = context()
    resource["mem_disk_mounts"][0][field] = value
    with pytest.raises(ValueError):
        parse_resource_context(resource)


@pytest.mark.parametrize("mount", ["/latest_frame", "/latest_frame/nested"])
def test_overlapping_mounts_rejected(mount):
    value = context()
    value["mem_disk_mounts"].append({"mount": mount,
        "volume": f'envtraverse-mem-{value["session_id"]}-1', "size_bytes": 10})
    with pytest.raises(ValueError, match="overlap"):
        parse_resource_context(value)


@pytest.mark.parametrize("raw", ["", "not JSON", "[]", '{"unknown":1}'])
def test_invalid_environment_is_not_silently_ignored(monkeypatch, raw):
    monkeypatch.setenv("ENVTRAVERSE_RESOURCES", raw)
    with pytest.raises(ValueError):
        read_resource_context()
