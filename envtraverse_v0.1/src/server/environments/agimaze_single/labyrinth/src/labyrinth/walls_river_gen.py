"""Stage-2 wall generation: river-adjacent walls.

We consider candidate edges between a river cell v and a non-river neighbor u.
Placing a wall blocks crossing that boundary.

Acceptance criteria (heuristic):
- Let v2 be the FINAL position reached when stepping from u into v (without the new wall).
  After adding the wall, require that u can still reach v2 (by some sequence of moves)
  and that v can still reach u, under the full movement rules (rivers etc.).
- Additionally, require "no sinks from start" (global check) periodically.

This keeps river entrances possible while making some river-adjacent cells harder to access.
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
class RiverWallsResult:
    candidates: int
    accepted: int
    mean_river_walls: float


def _is_river_cell(board: BoardLike, c: Coord) -> bool:
    for f in board.features.get(c, []):
        if getattr(f, "name", "") == "river":
            return True
    return False


def _river_cells(board: BoardLike) -> List[Coord]:
    out = []
    for r in range(board.n):
        for c in range(board.m):
            if _is_river_cell(board, (r, c)):
                out.append((r, c))
    return out


def _neighbors4(board: BoardLike, c: Coord) -> List[Coord]:
    from .generator import Dir

    out = []
    for d in Dir:
        nc = (c[0] + d.value[0], c[1] + d.value[1])
        if board.in_bounds(nc):
            out.append(nc)
    return out


def _mean_river_walls(board: BoardLike) -> float:
    river = _river_cells(board)
    if not river:
        return 0.0

    cnt = 0
    denom = 0
    for v in river:
        for u in _neighbors4(board, v):
            if _is_river_cell(board, u):
                continue
            denom += 1
            if board.walls.is_blocked(v, u):
                cnt += 1
    # denom counts river->nonriver adjacencies; divide by number of river cells to get mean walls per river cell
    return cnt / len(river)


def _reachable_pos(board: BoardLike, start: Coord, goal: Coord, *, max_visits: int = 200_000) -> bool:
    """BFS reachability in the full movement rules using attempt_move()."""
    from collections import deque

    from .generator import Dir, PlayerState, StepResult, attempt_move

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


def generate_river_adjacent_walls_v1(
    board: BoardLike,
    *,
    rng,
    start: Coord,
    target_mean_river_walls: float,
    global_check,
    global_check_period: int = 10,
) -> RiverWallsResult:
    from .generator import Dir, PlayerState, StepResult, attempt_move, WallSet

    river = _river_cells(board)
    if not river:
        return RiverWallsResult(candidates=0, accepted=0, mean_river_walls=0.0)

    # candidates are undirected edges (u,v) where v is river, u is non-river
    cands: List[Tuple[Coord, Coord]] = []
    for v in river:
        for u in _neighbors4(board, v):
            if _is_river_cell(board, u):
                continue
            # store as (u, v)
            cands.append((u, v))

    rng.shuffle(cands)

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

    for (u, v) in cands:
        # stop if target reached
        if _mean_river_walls(board) >= target_mean_river_walls:
            break

        # if already walled, skip
        if board.walls.is_blocked(u, v):
            continue

        # compute v2: where do we end up if we step from u into v (without the new wall)
        # find direction u->v
        duv = None
        for d in Dir:
            if (u[0] + d.value[0], u[1] + d.value[1]) == v:
                duv = d
                break
        if duv is None:
            continue

        s_u = PlayerState(pos=u)
        res, tr0 = attempt_move(board, s_u, duv)
        if res is StepResult.BLOCKED:
            # cannot enter river here anyway
            continue
        v2 = tr0.state.pos

        # try add wall
        set_wall(u, v, True)

        ok_local = _reachable_pos(board, u, v2) and _reachable_pos(board, v, u)
        ok_global = True

        if ok_local:
            accepted += 1
            checked_since += 1
            # periodic global check
            if checked_since >= global_check_period:
                checked_since = 0
                chk = global_check(board, start)
                ok_global = bool(getattr(chk, "ok", False))
        else:
            ok_global = False

        if not ok_local or not ok_global:
            # revert
            set_wall(u, v, False)
            accepted -= 1 if ok_local else 0

    return RiverWallsResult(candidates=len(cands), accepted=max(0, accepted), mean_river_walls=_mean_river_walls(board))
