"""Exit placement based on rarity of border cells.

We place an exit edge (cell + outward direction) after other key objects.
Heuristic: choose a border cell that is rare (low visitation count) under a long random walk.

Constraints:
- avoid specified forbidden cells (e.g., river sources and cells right after sources)
- if border cell is a corner, choose outward direction uniformly among outward sides

Note: in current engine, stepping out through the exit edge is represented as staying
on the exit cell, so opening an exit does not change position dynamics; it only affects
win condition and narration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol, Tuple

from .transitions import build_transition_table, idx_of
from .placement2 import estimate_rarity

Coord = Tuple[int, int]


class BoardLike(Protocol):
    n: int
    m: int
    exit_edge: object


@dataclass(frozen=True)
class ExitChoice:
    cell: Coord
    dir_name: str
    count: int


def choose_exit_edge(
    board: BoardLike,
    *,
    start: Coord,
    rng,
    forbidden: set[Coord] | None = None,
    burn_mult: int = 20,
    steps_mult: int = 400,
) -> ExitChoice:
    from .generator import Dir

    forbidden = set(forbidden or set())

    table = build_transition_table(board)  # type: ignore[arg-type]
    area = table.n * table.m
    rarity = estimate_rarity(table, start, burn=burn_mult * area, steps=steps_mult * area, rng=rng)

    # border cells
    border: List[Coord] = []
    n, m = table.n, table.m
    for r in range(n):
        for c in range(m):
            if r in (0, n - 1) or c in (0, m - 1):
                if (r, c) not in forbidden:
                    border.append((r, c))

    if not border:
        border = [(0, 0)]

    # choose rare (min count), prefer visited (>0) but allow 0 as fallback
    def cnt(cell: Coord) -> int:
        return rarity.counts[idx_of(n, m, cell)]

    visited = [c for c in border if cnt(c) > 0]
    pool = visited if visited else border

    minc = min(cnt(c) for c in pool)
    rare_cells = [c for c in pool if cnt(c) == minc]
    cell = rng.choice(rare_cells)

    r, c = cell
    outward: List[Dir] = []
    if r == 0:
        outward.append(Dir.UP)
    if r == n - 1:
        outward.append(Dir.DOWN)
    if c == 0:
        outward.append(Dir.LEFT)
    if c == m - 1:
        outward.append(Dir.RIGHT)

    d = rng.choice(outward)
    return ExitChoice(cell=cell, dir_name=d.name.lower(), count=cnt(cell))


def apply_exit_edge(board, choice: ExitChoice) -> None:
    from .generator import Dir

    dir_map = {"up": Dir.UP, "down": Dir.DOWN, "left": Dir.LEFT, "right": Dir.RIGHT}
    board.exit_edge = (choice.cell, dir_map[choice.dir_name])
