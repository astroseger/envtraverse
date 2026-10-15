"""Rivers: feature types and linear-river generation.

A river is a directed path of cells with:
- a source cell (has an outgoing direction)
- zero or more body cells (each has an outgoing direction)
- a mouth cell (no outgoing direction)

Rule (per Alexey spec): after ANY player action, if the player ends up on a river cell
(source/body/mouth), the current carries them downstream by 2 cells.
If the immediate next cell is the mouth, it carries by 1.

Rivers never branch (out-degree <= 1). Future: merging can be supported.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# Avoid importing generator at module import time to prevent circular imports.
# We'll use runtime imports and lightweight helpers.
from typing import Protocol

class BoardLike(Protocol):
    n: int
    m: int
    features: dict
    def in_bounds(self, c: tuple[int,int]) -> bool: ...

# Dir and PlayerState are imported lazily in methods.

def add(a: Coord, b: tuple[int,int]) -> Coord:
    return (a[0] + b[0], a[1] + b[1])

Coord = Tuple[int, int]


def _river_at(board: BoardLike, pos: Coord) -> Optional["RiverCell"]:
    for f in board.features.get(pos, []):
        if getattr(f, "name", None) == "river":
            return f  # type: ignore[return-value]
    return None


@dataclass(frozen=True)
class RiverCell:
    """A river terrain cell.

    kind: 'source' | 'body' | 'mouth'
    flow: direction to the next cell (None for mouth)
    """

    kind: str
    flow: Optional[object]  # Dir, but imported lazily
    name: str = "river"

    def on_enter(self, board: BoardLike, s):
        return s

    def carry_steps(self, board: BoardLike, s) -> int:
        if self.kind == "mouth" or self.flow is None:
            return 0
        step1 = add(s.pos, self.flow.value)  # type: ignore[union-attr]
        if not board.in_bounds(step1):
            return 0
        r1 = _river_at(board, step1)
        if r1 is not None and getattr(r1, "kind", None) == "mouth":
            return 1
        return 2

    def forced_move(self, board: BoardLike, s):
        # After any action, if player is on a river cell, current carries by 2 downstream.
        # If the immediate next cell is the mouth, carry by 1.
        if self.kind == "mouth" or self.flow is None:
            return None

        # Boat rule: if the player has a boat, the current does not carry them.
        if getattr(s, "has_boat", False):
            return None

        from .generator import PlayerState

        step1 = add(s.pos, self.flow.value)  # type: ignore[union-attr]
        if not board.in_bounds(step1):
            return None

        # If the current would pass through a pit, trigger it immediately.
        # This supports the (optional) "pits on river" mode in a natural way: being carried
        # through a pit cell makes you fall in, even if that cell would otherwise be only an intermediate.
        if any(getattr(f, "name", "") == "pit" for f in board.features.get(step1, [])):
            return s.copy(pos=step1)

        r1 = _river_at(board, step1)
        if r1 is not None and getattr(r1, "kind", None) == "mouth":
            return s.copy(pos=step1)

        if r1 is None or getattr(r1, "flow", None) is None:
            return s.copy(pos=step1)

        step2 = add(step1, r1.flow.value)  # type: ignore[union-attr]
        if not board.in_bounds(step2):
            return s.copy(pos=step1)

        return s.copy(pos=step2)


RiverFlowMap = Dict[Coord, Optional[object]]  # Dir | None; None means mouth


def extract_river_map(board: BoardLike) -> RiverFlowMap:
    mp: RiverFlowMap = {}
    for pos, feats in board.features.items():
        for f in feats:
            if getattr(f, "name", None) == "river":
                mp[pos] = getattr(f, "flow", None)
    return mp
