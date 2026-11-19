from types import SimpleNamespace
import json

import pytest

from life_minecraft import images


def test_prepared_images_are_resolved_to_ids_once(monkeypatch):
    refs = []
    def inspect(command, **kwargs):
        refs.append(command[-1])
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "id": "sha256:" + ("1" if command[-1].endswith("server") else "2") * 64,
            "labels": {images.PREPARED_LABEL: images.PREPARED_VERSION}}))
    monkeypatch.setattr(images.subprocess, "run", inspect)
    environment = {}
    resolved = images.pin_images(environment)
    assert refs == ["life-minecraft-server", "life-minecraft-client"]
    assert environment["MC_SERVER_IMAGE"] == "sha256:" + "1" * 64
    assert environment["MC_CLIENT_IMAGE"] == "sha256:" + "2" * 64
    assert environment["MC_CLIENT_IMAGE_REFERENCE"] == "life-minecraft-client"
    assert all(image["prepared"] for image in resolved.values())


def test_missing_image_is_distinct_from_unavailable_docker(monkeypatch):
    monkeypatch.setattr(images.subprocess, "run", lambda *args, **kwargs:
        SimpleNamespace(returncode=1, stdout="", stderr="Error: No such image: test"))
    assert images.inspect_image("test") is None
    with pytest.raises(RuntimeError, match="Missing Minecraft images"):
        images.pin_images({})
    monkeypatch.setattr(images.subprocess, "run", lambda *args, **kwargs:
        SimpleNamespace(returncode=1, stdout="", stderr="Cannot connect to Docker daemon"))
    with pytest.raises(RuntimeError, match="daemon"):
        images.inspect_image("test")
