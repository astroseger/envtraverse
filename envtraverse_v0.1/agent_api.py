"""Host-side contract between agent implementations and the run controllers."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class AgentMount:
    source: Path
    target: str
    read_only: bool = False


@dataclass(frozen=True)
class LaunchPreparation:
    command: list[str]
    mounts: list[AgentMount] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
