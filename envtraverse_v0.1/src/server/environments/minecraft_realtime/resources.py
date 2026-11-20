"""Shared controller resources and Minecraft's optional live-image mount."""
from __future__ import annotations

try:
    from runtime_resources import parse_resource_context, read_resource_context
except ModuleNotFoundError as error:
    if error.name != "runtime_resources":
        raise
    from src.server.runtime_resources import parse_resource_context, read_resource_context

WORKSPACE_FRAME_PATH = "/home/user/run/agent_run/latest_frame.jpg"
MEMORY_FRAME_MOUNT = "/latest_frame"
MEMORY_FRAME_PATH = MEMORY_FRAME_MOUNT + "/latest_frame.jpg"


def frame_volume(resources: dict) -> str | None:
    return next((entry["volume"] for entry in resources.get("mem_disk_mounts", [])
                 if entry["mount"] == MEMORY_FRAME_MOUNT), None)
