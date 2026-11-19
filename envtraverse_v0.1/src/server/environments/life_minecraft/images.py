"""Resolve reusable Minecraft images once and pin each runtime to immutable IDs."""
from __future__ import annotations

import json
import os
import subprocess

PREPARED_LABEL = "io.envtraverse.life_minecraft.prepared"
PREPARED_VERSION = "1"
DEFAULT_IMAGES = {"server": "life-minecraft-server", "client": "life-minecraft-client"}


def inspect_image(reference: str) -> dict | None:
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", '{"id":{{json .Id}},"labels":{{json .Config.Labels}}}', reference],
        capture_output=True, text=True, timeout=15)
    if result.returncode:
        if "No such image" in result.stderr or "No such object" in result.stderr:
            return None
        raise RuntimeError("Cannot inspect Docker images; check that the Docker daemon is available")
    image = json.loads(result.stdout)
    if not isinstance(image.get("id"), str) or not image["id"].startswith("sha256:"):
        raise RuntimeError("Docker returned an invalid image identity")
    return {"reference": reference, "id": image["id"],
            "prepared": (image.get("labels") or {}).get(PREPARED_LABEL) == PREPARED_VERSION}


def inspect_images(environment=None) -> dict[str, dict | None]:
    environment = os.environ if environment is None else environment
    return {service: inspect_image(environment.get(f"MC_{service.upper()}_IMAGE", default))
            for service, default in DEFAULT_IMAGES.items()}


def pin_images(environment=None) -> dict[str, dict]:
    """Mutate only the supplied runtime environment, preserving original references."""
    environment = os.environ if environment is None else environment
    images = inspect_images(environment)
    missing = [service for service, image in images.items() if image is None]
    if missing:
        raise RuntimeError("Missing Minecraft images: " + ", ".join(missing) + "; run host_runtime/setup.sh")
    for service, image in images.items():
        key = f"MC_{service.upper()}_IMAGE"
        image["reference"] = environment.get(key + "_REFERENCE", image["reference"])
        environment[key + "_REFERENCE"] = image["reference"]
        environment[key] = image["id"]
    return images
