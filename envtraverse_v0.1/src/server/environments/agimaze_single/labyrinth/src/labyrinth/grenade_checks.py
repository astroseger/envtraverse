"""Fast generation-time checks for grenade strategy 'seal-cell-v1'.

Goal: avoid full BFS over (pos, inventory, blown_edges).

We check two things:
1) grenade pack is reachable from start WITHOUT using grenades.
2) the sealed object can be reached by blasting the walls we added.

For (2), we don't need to enumerate blown states. Since seal-cell-v1 seals a single
cell by adding walls on its open edges, we can check if there exists a reachable
neighbor of that cell. If yes, the player can stand there and blast one of the
added walls to enter.

If added_walls > 1, the player might need multiple blasts, but as long as they can
get to some neighbor of the sealed cell, they can blast repeatedly.

This is a *sufficient* condition for solvability with grenades in this strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Set, Tuple

from .generator import Board, PlayerState
from .reachability import reachable_positions


Coord = Tuple[int, int]


@dataclass(frozen=True)
class GrenadeSealCheck:
    ok: bool
    reason: str = ""


def _neighbors4(board: Board, cell: Coord) -> Set[Coord]:
    r, c = cell
    out = set()
    for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
        nb = (r + dr, c + dc)
        if 0 <= nb[0] < board.n and 0 <= nb[1] < board.m:
            out.add(nb)
    return out


def check_seal_cell_v1(board: Board, *, start: PlayerState, sealed_cell: Coord, pack_pos: Coord, max_states: int = 200_000) -> GrenadeSealCheck:
    reach = reachable_positions(board, start, max_states=max_states)

    if pack_pos not in reach:
        return GrenadeSealCheck(False, "grenade_pack_not_reachable")

    # Need to be able to approach the sealed cell from at least one neighbor.
    nbs = _neighbors4(board, sealed_cell)
    if not (nbs & reach):
        return GrenadeSealCheck(False, "sealed_cell_not_approachable")

    return GrenadeSealCheck(True, "")
