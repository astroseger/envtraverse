"""Grenades generation: seal-cell strategy.

We implement strategy 'seal-cell-v1':
- Choose a key object cell (key/treasure/exit_key) with minimal current open degree.
- Add walls on its open edges to fully seal that cell.
- Place a grenade pack AFTER sealing so that it remains reachable.

We do not try to minimize the number of sealed regions; we only seal a single cell.
The number of grenades granted is: needed_walls + extra_grenades.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol, Tuple


Coord = Tuple[int, int]


class BoardLike(Protocol):
    n: int
    m: int
    walls: object
    features: dict
    def in_bounds(self, c: Coord) -> bool: ...


@dataclass
class SealResult:
    sealed_cell: Optional[Coord]
    pack_pos: Optional[Coord]
    added_walls: int
    grenades_given: int


def _neighbors4(pos: Coord) -> List[Tuple[Coord, Coord]]:
    r, c = pos
    return [((r - 1, c), pos), ((r + 1, c), pos), ((r, c - 1), pos), ((r, c + 1), pos)]


def _edge(u: Coord, v: Coord) -> Tuple[Coord, Coord]:
    return (u, v) if u <= v else (v, u)


def _has_feature(board: BoardLike, pos: Coord, name: str) -> bool:
    return any(getattr(f, "name", "") == name for f in board.features.get(pos, []))


def _open_degree(board: BoardLike, pos: Coord) -> int:
    from .generator import Dir, attempt_move, PlayerState, StepResult

    s = PlayerState(pos=pos)
    deg = 0
    for d in Dir:
        res, _ = attempt_move(board, s, d)
        if res is StepResult.OK:
            deg += 1
    return deg


def seal_cell_and_place_grenades(
    board: BoardLike,
    *,
    rng,
    extra_grenades: int = 1,
    max_walls_to_add: int = 3,
    forbid_seal: Optional[set[Coord]] = None,
    forbid_pack: Optional[set[Coord]] = None,
    start: Optional[Coord] = None,
) -> SealResult:
    """Seal a key-object cell by adding walls, then place a grenade pack.

    Important: we place the grenade pack on a cell that is reachable from start
    WITHOUT using any grenades, so the player can't get soft-locked.
    """

    from .generator import WallSet
    from .grenades import GrenadePack

    forbid_seal = forbid_seal or set()
    forbid_pack = forbid_pack or set()

    # collect candidate key-object cells
    key_cells: List[Coord] = []
    for pos, feats in board.features.items():
        for f in feats:
            if getattr(f, "name", "") in ("key", "treasure", "exit_key"):
                key_cells.append(pos)
                break

    # avoid sealing forbidden cells and dynamic terrains (river/pit) for now
    candidates = [
        c
        for c in key_cells
        if c not in forbid_seal
        and not _has_feature(board, c, "river")
        and not _has_feature(board, c, "pit")
    ]

    if not candidates:
        return SealResult(sealed_cell=None, pack_pos=None, added_walls=0, grenades_given=0)

    # pick minimal open degree (tie-break by deterministic sort)
    scored = []
    for c in candidates:
        scored.append((_open_degree(board, c), c))
    scored.sort(key=lambda t: (t[0], t[1]))

    deg, cell = scored[0]
    if deg <= 0:
        # already sealed by walls/monolith; do nothing
        return SealResult(sealed_cell=None, pack_pos=None, added_walls=0, grenades_given=0)

    # add walls on open edges to fully seal the cell
    # We'll modify WallSet by rebuilding its blocked set.
    blocked = set(getattr(board.walls, "blocked", frozenset()))

    from .generator import Dir, add
    added = 0
    for d in Dir:
        nb = add(cell, d.value)
        if not board.in_bounds(nb):
            continue
        # if already blocked, skip
        if getattr(board.walls, "is_blocked")(cell, nb):
            continue
        # add undirected wall => add both directed edges
        blocked.add((cell, nb))
        blocked.add((nb, cell))
        added += 1

    if added > int(max_walls_to_add):
        # revert (too hard)
        return SealResult(sealed_cell=None, pack_pos=None, added_walls=0, grenades_given=0)

    board.walls = WallSet(frozenset(blocked))

    grenades_given = max(0, added + int(extra_grenades))

    # place grenade pack AFTER sealing.
    # Choose a cell that is reachable from start WITHOUT using grenades.
    pack_pos = None
    if start is not None:
        from .reachability import reachable_positions
        from .generator import PlayerState

        reach = reachable_positions(board, PlayerState(pos=start), max_states=200_000)

        cands: List[Coord] = []
        for c in reach:
            if c in forbid_pack or c == cell:
                continue
            if _has_feature(board, c, "river") or _has_feature(board, c, "pit"):
                continue
            if _has_feature(board, c, "key") or _has_feature(board, c, "treasure") or _has_feature(board, c, "exit_key"):
                continue
            cands.append(c)

        if cands:
            pack_pos = rng.choice(cands)

    # Fallback: choose a random cell with the same constraints (may be unreachable; rare)
    if pack_pos is None:
        all_cells = [(r, c) for r in range(board.n) for c in range(board.m)]
        rng.shuffle(all_cells)
        for c in all_cells:
            if c in forbid_pack or c == cell:
                continue
            if _has_feature(board, c, "river") or _has_feature(board, c, "pit"):
                continue
            if _has_feature(board, c, "key") or _has_feature(board, c, "treasure") or _has_feature(board, c, "exit_key"):
                continue
            pack_pos = c
            break

    if pack_pos is not None and grenades_given > 0:
        board.features.setdefault(pack_pos, []).append(GrenadePack(count=grenades_given))

    return SealResult(sealed_cell=cell, pack_pos=pack_pos, added_walls=added, grenades_given=grenades_given)
