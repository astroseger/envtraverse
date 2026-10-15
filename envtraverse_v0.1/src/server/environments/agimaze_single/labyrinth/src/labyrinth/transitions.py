"""Fast transition table for position-only dynamics.

We precompute T[pos_index][action_index] = next_pos_index using attempt_move().
This makes random-walk simulations fast.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

# Avoid generator import cycles; use runtime imports.
from typing import Protocol

class BoardLike(Protocol):
    n: int
    m: int

Coord = Tuple[int, int]


# ACTIONS indices are 0..3, mapped to generator.Dir at runtime.


def idx_of(n: int, m: int, c: Coord) -> int:
    return c[0] * m + c[1]


def coord_of(n: int, m: int, i: int) -> Coord:
    return (i // m, i % m)


@dataclass(frozen=True)
class TransitionTable:
    n: int
    m: int
    nxt: List[List[int]]  # size: (n*m) x 4

    def step_idx(self, i: int, a: int) -> int:
        return self.nxt[i][a]


def build_transition_table(board: BoardLike) -> TransitionTable:
    # runtime imports
    from .generator import Dir, PlayerState, attempt_move

    n, m = board.n, board.m
    dirs = [Dir.UP, Dir.DOWN, Dir.LEFT, Dir.RIGHT]

    nxt = [[0] * 4 for _ in range(n * m)]
    for r in range(n):
        for c in range(m):
            u = (r, c)
            s = PlayerState(pos=u)
            ui = idx_of(n, m, u)
            for ai, d in enumerate(dirs):
                _, tr = attempt_move(board, s, d)
                nxt[ui][ai] = idx_of(n, m, tr.state.pos)
    return TransitionTable(n=n, m=m, nxt=nxt)
