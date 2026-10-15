"""Wall generation strategies.

Stage-1 walls: only between NON-RIVER cells, with a fast connectivity-preserving rule.

We model the non-river subgraph as an undirected graph (ignore river forced dynamics).
We add walls (remove edges) while keeping the non-river graph connected.

Target knob: mean available directions (degree) over non-river cells.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Set, Tuple

# Avoid generator import cycles; use Protocol + runtime Dir.
from typing import Protocol

Coord = Tuple[int, int]

class BoardLike(Protocol):
    n: int
    m: int
    features: dict
    walls: object
    def in_bounds(self, c: Coord) -> bool: ...

# Dir is imported lazily in functions.


@dataclass
class WallsGenResult:
    edges_considered: int
    walls_added: int
    mean_degree: float


def _is_river_cell(board: BoardLike, c: Coord) -> bool:
    for f in board.features.get(c, []):
        if getattr(f, "name", "") == "river":
            return True
    return False


def _neighbors4(board: BoardLike, c: Coord) -> List[Coord]:
    from .generator import Dir
    out = []
    for d in Dir:
        nc = (c[0] + d.value[0], c[1] + d.value[1])
        if board.in_bounds(nc):
            out.append(nc)
    return out


def _bfs_reaches(board: BoardLike, start: Coord, goal: Coord, *, allowed: Set[Coord]) -> bool:
    """BFS in the *current* open-edge graph, restricted to allowed vertices."""
    from collections import deque

    if start == goal:
        return True

    q = deque([start])
    seen = {start}

    while q:
        u = q.popleft()
        for v in _neighbors4(board, u):
            if v not in allowed:
                continue
            if board.walls.is_blocked(u, v):
                continue
            if v in seen:
                continue
            if v == goal:
                return True
            seen.add(v)
            q.append(v)

    return False


def _mean_degree(board: BoardLike, cells: List[Coord]) -> float:
    if not cells:
        return 0.0
    deg_sum = 0
    s = set(cells)
    for c in cells:
        deg = 0
        for v in _neighbors4(board, c):
            if v in s and not board.walls.is_blocked(c, v):
                deg += 1
        deg_sum += deg
    return deg_sum / len(cells)


def generate_walls_degree_v1(
    board: BoardLike,
    *,
    rng,
    target_mean_degree: float,
) -> WallsGenResult:
    """Add walls between non-river cells until mean degree is near target.

    Fast approach:
    - consider each undirected edge between non-river cells in random order
    - try to block it (both directions)
    - keep it blocked only if connectivity between its endpoints remains (BFS u->v)

    This preserves connectivity of the non-river subgraph.
    """

    non_river = [(r, c) for r in range(board.n) for c in range(board.m) if not _is_river_cell(board, (r, c))]
    allowed = set(non_river)

    # list undirected candidate edges (u<v lexicographically)
    edges: List[Tuple[Coord, Coord]] = []
    for u in non_river:
        for v in _neighbors4(board, u):
            if v not in allowed:
                continue
            if u < v:
                edges.append((u, v))

    rng.shuffle(edges)

    blocked = set(board.walls.blocked)
    walls_added = 0
    considered = 0

    def block_edge(u: Coord, v: Coord) -> None:
        blocked.add((u, v))
        blocked.add((v, u))

    def unblock_edge(u: Coord, v: Coord) -> None:
        blocked.discard((u, v))
        blocked.discard((v, u))

    # temporarily patch board.walls for BFS checks by swapping WallSet
    from .generator import WallSet

    for (u, v) in edges:
        considered += 1
        # stop if already sparse enough
        board.walls = WallSet(frozenset(blocked))
        cur_deg = _mean_degree(board, non_river)
        if cur_deg <= target_mean_degree:
            break

        # try block
        block_edge(u, v)
        board.walls = WallSet(frozenset(blocked))

        if _bfs_reaches(board, u, v, allowed=allowed):
            walls_added += 1
        else:
            # would disconnect -> revert
            unblock_edge(u, v)

    board.walls = WallSet(frozenset(blocked))
    md = _mean_degree(board, non_river)

    return WallsGenResult(edges_considered=considered, walls_added=walls_added, mean_degree=md)
