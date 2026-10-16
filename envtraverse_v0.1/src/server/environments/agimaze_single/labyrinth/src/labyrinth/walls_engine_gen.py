"""Stage-1.5 wall generation: non-river walls using engine reachability.

Motivation:
- Stage-1 walls keep the non-river subgraph connected WITHOUT using rivers. This can be too soft.
- Here we add more walls between non-river cells, but we allow alternative connectivity via rivers
  by using full engine transitions (attempt_move) in reachability checks.

Invariant:
- preserve "no sinks from start" (global check) periodically.

Local acceptance heuristic:
- when blocking edge between adjacent non-river cells u and v:
  require u can still reach v and v can still reach u under engine transitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Protocol, Set, Tuple

Coord = Tuple[int, int]


class BoardLike(Protocol):
    n: int
    m: int
    features: dict
    walls: object
    def in_bounds(self, c: Coord) -> bool: ...


@dataclass
class EngineWallsResult:
    candidates: int
    accepted: int
    mean_degree_nonriver: float


def _is_river_cell(board: BoardLike, c: Coord) -> bool:
    for f in board.features.get(c, []):
        if getattr(f, "name", "") == "river":
            return True
    return False


def _non_river_cells(board: BoardLike) -> List[Coord]:
    return [(r, c) for r in range(board.n) for c in range(board.m) if not _is_river_cell(board, (r, c))]


def _neighbors4(board: BoardLike, c: Coord) -> List[Coord]:
    from .generator import Dir

    out = []
    for d in Dir:
        nc = (c[0] + d.value[0], c[1] + d.value[1])
        if board.in_bounds(nc):
            out.append(nc)
    return out


def _mean_degree_nonriver(board: BoardLike) -> float:
    cells = _non_river_cells(board)
    if not cells:
        return 0.0
    s = set(cells)
    deg_sum = 0
    for u in cells:
        deg = 0
        for v in _neighbors4(board, u):
            if v in s and not board.walls.is_blocked(u, v):
                deg += 1
        deg_sum += deg
    return deg_sum / len(cells)


def _reachable_pos(board: BoardLike, start: Coord, goal: Coord, *, max_visits: int = 200_000) -> bool:
    from collections import deque
    from .generator import Dir, PlayerState, attempt_move

    if start == goal:
        return True

    q = deque([start])
    seen = {start}

    while q and len(seen) <= max_visits:
        pos = q.popleft()
        s = PlayerState(pos=pos)
        for d in Dir:
            _, tr = attempt_move(board, s, d)
            nxt = tr.state.pos
            if nxt == goal:
                return True
            if nxt not in seen:
                seen.add(nxt)
                q.append(nxt)

    return False


def generate_nonriver_walls_engine_v1(
    board: BoardLike,
    *,
    rng,
    start: Coord,
    target_mean_degree: float,
    global_check,
    global_check_period: int = 10,
) -> EngineWallsResult:
    """Add extra walls between non-river cells, using engine reachability acceptance."""

    from .generator import WallSet

    non_river = _non_river_cells(board)
    s_non = set(non_river)

    # undirected candidate edges u<v
    edges: List[Tuple[Coord, Coord]] = []
    for u in non_river:
        for v in _neighbors4(board, u):
            if v in s_non and u < v:
                edges.append((u, v))

    rng.shuffle(edges)

    blocked = set(board.walls.blocked)

    def set_wall(u: Coord, v: Coord, on: bool) -> None:
        if on:
            blocked.add((u, v))
            blocked.add((v, u))
        else:
            blocked.discard((u, v))
            blocked.discard((v, u))
        board.walls = WallSet(frozenset(blocked))

    accepted = 0
    checked_since = 0

    for (u, v) in edges:
        if _mean_degree_nonriver(board) <= target_mean_degree:
            break
        if board.walls.is_blocked(u, v):
            continue

        # try wall
        set_wall(u, v, True)

        ok_local = _reachable_pos(board, u, v) and _reachable_pos(board, v, u)
        ok_global = True

        if ok_local:
            accepted += 1
            checked_since += 1
            if checked_since >= global_check_period:
                checked_since = 0
                chk = global_check(board, start)
                ok_global = bool(getattr(chk, "ok", False))
        else:
            ok_global = False

        if not ok_local or not ok_global:
            set_wall(u, v, False)
            accepted -= 1 if ok_local else 0

    return EngineWallsResult(candidates=len(edges), accepted=max(0, accepted), mean_degree_nonriver=_mean_degree_nonriver(board))
