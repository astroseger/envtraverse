"""Host-local rendezvous between runtime sessions and a Harbor custom agent.

The registry is NOT a network endpoint and is never exposed to task containers.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .settings import TerminalSettings
from .lifecycle import SessionLifecycle


@dataclass
class BridgeState:
    settings: TerminalSettings
    workspace_dir: Path
    upload_dir: Path
    lifecycle: SessionLifecycle = field(default_factory=SessionLifecycle)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    instruction: str | None = None
    initial_observation: dict[str, Any] | None = None
    logs_dir: Path | None = None
    finish_reason: str = "finish"


BRIDGES: dict[str, BridgeState] = {}
