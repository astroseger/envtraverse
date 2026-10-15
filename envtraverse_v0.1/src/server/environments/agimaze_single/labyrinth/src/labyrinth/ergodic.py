"""Ergodicity / sink detection for the base game (position-only).

We treat the game as a directed graph over positions:
- vertices: all cells (r,c)
- edges: for each position and each action in {U,D,L,R}, apply attempt_move (with rivers)
         and connect pos -> final_pos.

We say there are no "sinks from start" if the reachable set from start is strongly connected,
(i.e. every reachable vertex can reach back to start).

This matches the requirement: no irreversible traps reachable from start.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Set, Tuple

# Avoid generator import cycles; use runtime imports.
from typing import Protocol

class BoardLike(Protocol):
    n: int
    m: int



def transition_edges(board: BoardLike) -> Dict[Coord, List[Coord]]:
    # runtime imports to avoid circular deps
    from .generator import Dir, PlayerState, attempt_move
    edges: Dict[Coord, List[Coord]] = {}
    for r in range(board.n):
        for c in range(board.m):
            u = (r, c)
            outs: List[Coord] = []
            s = PlayerState(pos=u)
            for d in Dir:
                res, tr = attempt_move(board, s, d)
                # blocked stays in place
                outs.append(tr.state.pos)
            edges[u] = outs
    return edges


def reachable_from(edges: Dict[Coord, List[Coord]], start: Coord) -> Set[Coord]:
    stack = [start]
    seen = {start}
    while stack:
        u = stack.pop()
        for v in edges.get(u, []):
            if v not in seen:
                seen.add(v)
                stack.append(v)
    return seen


def reverse_edges(edges: Dict[Coord, List[Coord]]) -> Dict[Coord, List[Coord]]:
    rev: Dict[Coord, List[Coord]] = {u: [] for u in edges}
    for u, outs in edges.items():
        for v in outs:
            rev.setdefault(v, []).append(u)
    return rev


@dataclass(frozen=True)
class ErgodicCheck:
    ok: bool
    reachable: int
    not_returnable: int
    example_not_returnable: Coord | None = None


def check_no_sinks_from_start(board: BoardLike, start: Coord) -> ErgodicCheck:
    edges = transition_edges(board)
    fwd = reachable_from(edges, start)

    rev = reverse_edges(edges)
    back = reachable_from(rev, start)

    # Any vertex that is reachable from start but cannot reach start is a "sink-reachable" symptom.
    bad = fwd - back
    ex = next(iter(bad)) if bad else None
    return ErgodicCheck(ok=(len(bad) == 0), reachable=len(fwd), not_returnable=len(bad), example_not_returnable=ex)
