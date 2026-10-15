"""Grenades: pickup + wall blasting.

Design:
- Grenades are stored as an integer counter in PlayerState (grenades).
- A grenade pack is a Feature placed on a cell; when the player enters the cell,
  they pick it up automatically.

Blasting walls is implemented as separate player actions at the Engine/solver level.
We do NOT model walls as a Feature; instead, the state contains a set of blown edges.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Tuple


Coord = Tuple[int, int]


class BoardLike(Protocol):
    features: dict


@dataclass(frozen=True)
class GrenadePack:
    count: int = 2
    name: str = "grenade_pack"

    def on_enter(self, board: BoardLike, s):
        """Pickup model used by solver/generator.

        Board features are immutable during solving, so we must avoid granting grenades
        repeatedly on revisits.

        We assume (current design) there is at most one grenade pack on the board.
        Therefore we model pickup as setting grenades to at least `count`.
        The text/UI engine consumes the pack separately.
        """
        from .generator import PlayerState

        if self.count <= 0:
            return s

        new_gren = max(int(s.grenades), int(self.count))
        if new_gren == s.grenades:
            return s

        return s.copy(grenades=new_gren)

    def forced_move(self, board: BoardLike, s):
        return None
