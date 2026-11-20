"""Adapter errors around the bridge's canonical input schema."""
from __future__ import annotations

from typing import Any
from .game.bridge.actions import BUTTONS, KEYS, validate_action as _validate_action
from .protocol import RuntimeFault


def validate_action(action: Any, *, width: int = 960, height: int = 540) -> dict[str, Any]:
    try:
        return _validate_action(action, width=width, height=height)
    except ValueError as exc:
        raise RuntimeFault("invalid_action", str(exc)) from exc
