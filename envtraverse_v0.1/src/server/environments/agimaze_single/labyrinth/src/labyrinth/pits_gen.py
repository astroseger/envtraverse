"""Pit placement: simple heuristic generator.

We place one or more disjoint pit cycles on the board.

Constraints:
- Pit cells must be in bounds and distinct.
- A pit cell must NOT be surrounded by 4 blocked moves in the *current* board dynamics.
  (Per spec: if you are in a pit and you try to move but hit a wall, you shouldn't
  immediately fall again; so at least one direction should be available.)

Notes:
- We allow pits to be on land or on river cells (interesting combos).
- We recommend running pit placement AFTER walls/rivers (so we can check 4-walls constraint).
- We recommend placing pits BEFORE key objects (so item placement accounts for pit dynamics).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol, Tuple


Coord = Tuple[int, int]


class BoardLike(Protocol):
    n: int
    m: int
    features: dict


@dataclass(frozen=True)
class PitsConfig:
    enabled: bool = False
    strategy: str = "cycles-v1"

    # number of independent cycles (>=1 if enabled)
    cycles: int = 1
    # fixed number of pits in each cycle
    cycle_len: int = 3

    # whether pits may be placed on river cells (default: no)
    allow_on_river: bool = False


@dataclass
class PitGenResult:
    placed_cells: int
    cycles: int


def _has_feature(board: BoardLike, pos: Coord, name: str) -> bool:
    return any(getattr(f, "name", "") == name for f in board.features.get(pos, []))


def _is_river(board: BoardLike, pos: Coord) -> bool:
    return _has_feature(board, pos, "river")


def _is_dead_end_cell(board: BoardLike, pos: Coord) -> bool:
    """True if all 4 actions leave you in the same cell.

    We check with the transition table, which includes forced effects.
    If a cell has walls/monolith on all sides, it is a dead-end for movement.
    """
    from .transitions import build_transition_table, idx_of

    table = build_transition_table(board)
    i = idx_of(table.n, table.m, pos)
    return all(table.step_idx(i, a) == i for a in range(4))


def place_pit_cycles(
    board: BoardLike,
    *,
    rng,
    cfg: PitsConfig,
    forbid: Optional[set[Coord]] = None,
) -> PitGenResult:
    if not cfg.enabled:
        return PitGenResult(placed_cells=0, cycles=0)

    n, m = board.n, board.m
    area = n * m
    forbid = forbid or set()

    # Candidate cells: any cell not forbidden and not already a pit.
    all_cells: List[Coord] = [(r, c) for r in range(n) for c in range(m)]

    if cfg.strategy != "cycles-v1":
        raise ValueError(f"Unknown pits strategy: {cfg.strategy}")

    # Choose target number of pit cells: fixed cycles * cycle_len
    # Allow cycle_len=1 (self-loop) if user wants it.
    L = max(1, int(cfg.cycle_len))
    k = max(1, int(cfg.cycles))
    target = min(k * L, area)

    # Pre-filter: must not be a dead-end cell.
    candidates = [
        c
        for c in all_cells
        if c not in forbid
        and not _has_feature(board, c, "pit")
        and not _is_dead_end_cell(board, c)
        and (cfg.allow_on_river or not _is_river(board, c))
    ]
    rng.shuffle(candidates)

    from .pits import Pit

    placed = 0
    cycles_done = 0

    # build cycles greedily
    while cycles_done < k and candidates and placed < target:
        if L > len(candidates):
            break

        cycle_cells = [candidates.pop() for _ in range(L)]

        # link them in a cycle
        for i, cell in enumerate(cycle_cells):
            nxt = cycle_cells[(i + 1) % L]
            board.features.setdefault(cell, []).append(Pit(next_pos=nxt, cycle_id=cycles_done, idx_in_cycle=i))

        placed += L
        cycles_done += 1

    return PitGenResult(placed_cells=placed, cycles=cycles_done)
