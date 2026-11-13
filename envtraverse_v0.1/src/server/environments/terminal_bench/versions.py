"""Agent-facing contracts, independent of dataset and wire-protocol versions."""
from __future__ import annotations

from dataclasses import dataclass


DEFAULT_ENVIRONMENT_VERSION = "v1"
_COMMON_ACTIONS = frozenset({
    "terminal_new", "terminal_send", "terminal_read", "send_path", "finish",
})


@dataclass(frozen=True)
class EnvironmentVersion:
    name: str
    retrieval_action: str

    @property
    def actions(self) -> frozenset[str]:
        return _COMMON_ACTIONS | {self.retrieval_action}


VERSIONS = {
    "v1": EnvironmentVersion("v1", "get_file"),
    "v1.2": EnvironmentVersion("v1.2", "get_path"),
}


def get_version(value: str = DEFAULT_ENVIRONMENT_VERSION) -> EnvironmentVersion:
    if not isinstance(value, str) or value not in VERSIONS:
        raise ValueError("environment_version must be one of: " + ", ".join(VERSIONS))
    return VERSIONS[value]
