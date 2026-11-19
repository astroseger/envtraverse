"""Controller-owned resources available to environments and their host runtimes.

The launcher supplies ENVTRAVERSE_RESOURCES only to the environment server.
Adapters may forward this JSON context to an external runtime; it is separate
from public environment parameters and grants no Docker access to the agent.
"""
from __future__ import annotations

import json
import os
from pathlib import PurePosixPath
import re
from typing import Any

RESOURCES_VARIABLE = "ENVTRAVERSE_RESOURCES"


def parse_resource_context(value: Any, *, session_id: str | None = None) -> dict:
    """Validate an optional resource context, including per-session ownership."""
    if value is None or value == {}:
        return {}
    if not isinstance(value, dict) or set(value) != {"session_id", "mem_disk_mounts"}:
        raise ValueError("Invalid runtime resource context")
    owner = value["session_id"]
    if not isinstance(owner, str) or re.fullmatch(r"[a-f0-9]{32}", owner) is None:
        raise ValueError("Resource session_id must be a hexadecimal UUID")
    if session_id is not None and owner != session_id:
        raise ValueError("Resources belong to a different session")
    entries = value["mem_disk_mounts"]
    if not isinstance(entries, list):
        raise ValueError("Resource mem_disk_mounts must be a list")
    mounts, volumes, result = [], set(), []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"mount", "volume", "size_bytes"}:
            raise ValueError("Invalid memory volume resource")
        mount, volume, size = entry["mount"], entry["volume"], entry["size_bytes"]
        if (not isinstance(mount, str) or not mount.startswith("/") or mount.startswith("//")
                or any(c in mount for c in ",:\0\r\n") or mount == "/"):
            raise ValueError("Memory volume mount must be an absolute container directory")
        path = PurePosixPath(mount)
        if str(path) != mount or ".." in path.parts:
            raise ValueError("Memory volume mount must use a canonical path")
        if any(path == prior or path in prior.parents or prior in path.parents for prior in mounts):
            raise ValueError("Memory volume mount paths overlap")
        if not isinstance(volume, str) or re.fullmatch(r"envtraverse-mem-" + owner + r"-[0-9]+", volume) is None:
            raise ValueError("Memory volume name does not belong to this session")
        if volume in volumes:
            raise ValueError("Duplicate memory volume name")
        if type(size) is not int or not 0 < size <= 2**63 - 1:
            raise ValueError("Memory volume size_bytes must be a positive integer")
        mounts.append(path)
        volumes.add(volume)
        result.append(dict(entry))
    return {"session_id": owner, "mem_disk_mounts": result}


def read_resource_context(*, session_id: str | None = None) -> dict:
    """Read resources inside the server; absent configuration is an empty dict."""
    raw = os.environ.get(RESOURCES_VARIABLE)
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, TypeError) as error:
        raise ValueError("Invalid ENVTRAVERSE_RESOURCES JSON") from error
    return parse_resource_context(value, session_id=session_id)
