"""Convert environment observations into the client's JSON artifact format."""

from __future__ import annotations

import base64

from environment_api import Observation


def serialize_observation(observation: Observation) -> list[dict[str, str]]:
    if not isinstance(observation, Observation):
        raise TypeError("Environment manager must return an Observation")
    if type(observation.terminal) is not bool:
        raise TypeError("Observation.terminal must be a boolean")
    if not isinstance(observation.files, dict):
        raise TypeError("Observation.files must be a dictionary of filenames to str or bytes")

    artifacts = []
    for name, content in observation.files.items():
        if (
            not isinstance(name, str)
            or name in {"", ".", ".."}
            or any(character in name for character in "/\\\0")
        ):
            raise ValueError(f"Invalid observation filename: {name!r}; use a filename without directories")
        if isinstance(content, str):
            content = content.encode("utf-8")
        elif not isinstance(content, bytes):
            raise TypeError(f"Observation file {name!r} must contain str or bytes")
        artifacts.append({"name": name, "content_base64": base64.b64encode(content).decode("ascii")})
    return artifacts
