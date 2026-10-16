"""Pits: cyclic teleport terrain.

Rule (per Alexey spec):
- Pits are arranged into one or more disjoint cycles.
- When you step onto a pit, you immediately emerge from the next pit in its cycle
  (last -> first).
- If you are standing on a pit and your attempted move is BLOCKED by a wall/monolith,
  you do NOT fall again in that same turn. (Unlike rivers.)

Implementation note:
- We implement pits as a forced-move feature (name='pit').
- Since forced effects are only resolved after a successful movement in attempt_move(),
  pits will NOT trigger on blocked moves by default. This matches the spec.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Tuple


Coord = Tuple[int, int]


class BoardLike(Protocol):
    n: int
    m: int
    features: dict


@dataclass(frozen=True)
class Pit:
    """A pit cell that teleports to the next pit in its cycle."""

    next_pos: Coord
    cycle_id: int = 0
    idx_in_cycle: int = 0  # 0-based index within the cycle (for debugging/rendering)
    name: str = "pit"

    def on_enter(self, board: BoardLike, s):
        return s

    def forced_move(self, board: BoardLike, s):
        from .generator import PlayerState

        return s.copy(pos=self.next_pos)
