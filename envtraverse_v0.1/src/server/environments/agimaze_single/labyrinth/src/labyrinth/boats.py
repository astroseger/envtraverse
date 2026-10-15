"""Boat: optional item that prevents river current from carrying the player.

Rule:
- When the player has a boat, river forced-move does not apply.
- Boat is a pickup placed on a tile; picked up automatically on enter.

This is designed to be optional: if the boat system is disabled, the field is
not shown in observations/UI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class BoardLike(Protocol):
    features: dict


@dataclass(frozen=True)
class Boat:
    name: str = "boat"

    def on_enter(self, board: BoardLike, s):
        from .generator import PlayerState

        if getattr(s, "has_boat", False):
            return s
        return s.copy(has_boat=True)

    def forced_move(self, board: BoardLike, s):
        return None
