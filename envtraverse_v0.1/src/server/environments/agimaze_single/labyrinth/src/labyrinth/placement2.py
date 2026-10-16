"""Universal placement using rarity + incremental penalty maps.

Rarity is estimated from a long random-walk from start.
Penalty map is incrementally updated from anchors (start + already placed objects)
using short walks. This discourages placing multiple key objects in the same pocket
and avoids placing objects too close to start.

Policy: each step chooses uniformly among 4 actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# Avoid generator import cycles; use Protocol.
from typing import Protocol

class BoardLike(Protocol):
    n: int
    m: int
    features: dict
from .transitions import TransitionTable, build_transition_table, coord_of, idx_of

Coord = Tuple[int, int]


def _is_river(board: BoardLike, c: Coord) -> bool:
    return any(getattr(f, "name", "") == "river" for f in board.features.get(c, []))


def _valid_item_cell(board: BoardLike, c: Coord, *, start: Coord, exit_cell: Coord, occupied: set[Coord]) -> bool:
    if c == start or c == exit_cell:
        return False
    if c in occupied:
        return False
    if _is_river(board, c):
        return False
    # key objects should not be placed on pits
    if any(getattr(f, "name", "") == "pit" for f in board.features.get(c, [])):
        return False
    return True


@dataclass(frozen=True)
class Rarity:
    counts: List[int]  # per cell index
    steps: int


def estimate_rarity(table: TransitionTable, start: Coord, *, burn: int, steps: int, rng) -> Rarity:
    n, m = table.n, table.m
    pos = idx_of(n, m, start)

    for _ in range(max(0, burn)):
        a = rng.randrange(4)
        pos = table.step_idx(pos, a)

    counts = [0] * (n * m)
    for _ in range(max(0, steps)):
        a = rng.randrange(4)
        pos = table.step_idx(pos, a)
        counts[pos] += 1

    return Rarity(counts=counts, steps=steps)


def update_penalty_sum(
    table: TransitionTable,
    anchor: Coord,
    penalty: List[float],
    *,
    walks: int,
    length: int,
    rng,
) -> None:
    """Increment penalty by sum of 1/t for first-hit times within short walks."""
    n, m = table.n, table.m
    a0 = idx_of(n, m, anchor)

    for _ in range(walks):
        pos = a0
        seen_local: Dict[int, int] = {}
        for t in range(1, max(1, length) + 1):
            act = rng.randrange(4)
            pos = table.step_idx(pos, act)
            if pos not in seen_local:
                seen_local[pos] = t
        for i, t in seen_local.items():
            penalty[i] += 1.0 / float(t)


@dataclass
class PlacementPlan:
    order: List[str]  # e.g. ["key", "treasure", "boat"]


@dataclass
class Placed:
    positions: Dict[str, Coord]


def place_objects(
    board: BoardLike,
    *,
    start: Coord,
    exit_cell: Coord,
    rng,
    plan: PlacementPlan = PlacementPlan(order=["key", "treasure"]),
    walks_per_anchor: int = 20,
    penalty_walk_len: Optional[int] = None,
) -> Placed:
    table = build_transition_table(board)
    n, m = table.n, table.m

    area = n * m
    burn = 20 * area
    steps = 400 * area

    rarity = estimate_rarity(table, start, burn=burn, steps=steps, rng=rng)

    penalty = [0.0] * (n * m)
    L = penalty_walk_len if penalty_walk_len is not None else (n + m)

    occupied: set[Coord] = set()
    placed: Dict[str, Coord] = {}

    # anchors start with start
    anchors: List[Coord] = [start]
    update_penalty_sum(table, start, penalty, walks=walks_per_anchor, length=L, rng=rng)

    # candidate pool precomputed on land
    land_cells = [coord_of(n, m, i) for i in range(n * m) if not _is_river(board, coord_of(n, m, i))]

    alpha = 100.0

    def choose_cell() -> Coord:
        # Soft scoring: prefer rare cells, but penalize cells that are quickly reachable from anchors.
        # score = cnt * (1 + alpha * penalty)
        cands: List[Tuple[float, int, float, Coord]] = []  # (score, cnt, penalty, cell)
        for c in land_cells:
            if not _valid_item_cell(board, c, start=start, exit_cell=exit_cell, occupied=occupied):
                continue
            i = idx_of(n, m, c)
            cnt = rarity.counts[i]
            if cnt <= 0:
                continue
            p = penalty[i]
            score = float(cnt) * (1.0 + alpha * p)
            cands.append((score, cnt, p, c))

        if not cands:
            # fallback: allow cnt==0
            for c in land_cells:
                if not _valid_item_cell(board, c, start=start, exit_cell=exit_cell, occupied=occupied):
                    continue
                i = idx_of(n, m, c)
                cnt = rarity.counts[i]
                p = penalty[i]
                score = float(cnt + 1) * (1.0 + alpha * p)
                cands.append((score, cnt, p, c))

        cands.sort(key=lambda t: (t[0], t[1], t[2]))
        topk = min(2, len(cands))
        return rng.choice([c for _, _, _, c in cands[:topk]])

    for name in plan.order:
        pos = choose_cell()
        placed[name] = pos
        occupied.add(pos)
        anchors.append(pos)
        update_penalty_sum(table, pos, penalty, walks=walks_per_anchor, length=L, rng=rng)

    # materialize features
    from .generator import Key, Treasure, ExitKey
    from .boats import Boat

    if "key" in placed:
        board.features.setdefault(placed["key"], []).append(Key())
    if "treasure" in placed:
        board.features.setdefault(placed["treasure"], []).append(Treasure())
    if "exit_key" in placed:
        board.features.setdefault(placed["exit_key"], []).append(ExitKey())
    if "boat" in placed:
        board.features.setdefault(placed["boat"], []).append(Boat())

    return Placed(positions=placed)
