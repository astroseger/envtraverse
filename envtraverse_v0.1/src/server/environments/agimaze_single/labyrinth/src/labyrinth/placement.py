"""Placement of key objects (key, treasure, etc.) based on random-walk statistics.

Policy assumption (per Alexey): player chooses uniformly from 4 actions.
We estimate a stationary-like visitation frequency by a long random walk.

Goals (base game):
- treasure should be easier to find than key
- avoid placing key+treasure in the same hard-to-reach "pocket"

This module is heuristic; strict guarantees are enforced separately (no sinks from start).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# Avoid generator import cycles; import runtime symbols inside functions.
from typing import Protocol

class BoardLike(Protocol):
    n: int
    m: int
    features: dict



@dataclass(frozen=True)
class WalkStats:
    burn: int
    steps: int
    counts: Dict[Coord, int]


def random_walk_counts(board: BoardLike, start: Coord, *, burn: int, steps: int, rng) -> WalkStats:
    from .generator import Dir, PlayerState, attempt_move

    pos = start

    # burn-in
    for _ in range(max(0, burn)):
        d = rng.choice(list(Dir))
        _, tr = attempt_move(board, PlayerState(pos=pos), d)
        pos = tr.state.pos

    counts: Dict[Coord, int] = {start: 0}
    for _ in range(max(0, steps)):
        d = rng.choice(list(Dir))
        _, tr = attempt_move(board, PlayerState(pos=pos), d)
        pos = tr.state.pos
        counts[pos] = counts.get(pos, 0) + 1

    return WalkStats(burn=burn, steps=steps, counts=counts)


def short_hit_prob(board: BoardLike, start: Coord, target: Coord, *, trials: int, length: int, rng) -> float:
    from .generator import Dir, PlayerState, attempt_move

    hits = 0
    for _ in range(trials):
        pos = start
        ok = False
        for _ in range(length):
            d = rng.choice(list(Dir))
            _, tr = attempt_move(board, PlayerState(pos=pos), d)
            pos = tr.state.pos
            if pos == target:
                ok = True
                break
        if ok:
            hits += 1
    return hits / max(1, trials)


@dataclass(frozen=True)
class PlacementResult:
    key_pos: Optional[Coord]
    treasure_pos: Optional[Coord]


def place_key_and_treasure(
    board: BoardLike,
    *,
    start: Coord,
    exit_cell: Coord,
    rng,
    forbid: set[Coord] | None = None,
) -> PlacementResult:
    forbid = set(forbid or set())

    # candidate cells: non-river only (items on land), and not start/exit
    def is_river(c: Coord) -> bool:
        return any(getattr(f, "name", "") == "river" for f in board.features.get(c, []))

    cells: List[Coord] = [
        (r, c)
        for r in range(board.n)
        for c in range(board.m)
        if (r, c) not in forbid and (r, c) != start and (r, c) != exit_cell and not is_river((r, c))
    ]
    if not cells:
        return PlacementResult(key_pos=None, treasure_pos=None)

    area = board.n * board.m
    burn = 20 * area
    steps = 400 * area

    ws = random_walk_counts(board, start, burn=burn, steps=steps, rng=rng)

    # ensure all candidates have a count (0 if never visited)
    cnt = {c: ws.counts.get(c, 0) for c in cells}

    # KEY: choose among rare-but-visited (count > 0), minimizing count
    visited = [c for c in cells if cnt[c] > 0]
    if not visited:
        # if walk didn't reach land (unlikely), fallback random
        key_pos = rng.choice(cells)
    else:
        minc = min(cnt[c] for c in visited)
        rare = [c for c in visited if cnt[c] == minc]
        key_pos = rng.choice(rare)

    # TREASURE: should be easier than key => require count >= key_count * factor
    key_count = cnt.get(key_pos, 0)
    factor = 2.0
    treasure_pool = [c for c in cells if c != key_pos and cnt.get(c, 0) >= int(key_count * factor)]
    if not treasure_pool:
        # if too strict, relax
        treasure_pool = [c for c in cells if c != key_pos and cnt.get(c, 0) >= key_count]
    if not treasure_pool:
        treasure_pool = [c for c in cells if c != key_pos]

    # penalties based on short walks:
    # - avoid treasure being too easy to hit from start (not right next to start / in same tiny pocket)
    # - avoid treasure being too easy to hit from key (don't put both in same pocket)
    trials = 25
    length = 10 * area
    scored: List[Tuple[float, float, Coord]] = []  # (hit_from_start, hit_from_key, cell)
    for c in treasure_pool:
        hs = short_hit_prob(board, start, c, trials=trials, length=length, rng=rng)
        hk = short_hit_prob(board, key_pos, c, trials=trials, length=length, rng=rng)
        scored.append((hs, hk, c))

    # Prefer small hit probabilities (harder to reach quickly), but still keep treasure overall easier than key via count constraint.
    scored.sort(key=lambda t: (t[0], t[1], -cnt.get(t[2], 0)))
    topk = min(7, len(scored))
    treasure_pos = rng.choice([c for _, _, c in scored[:topk]])

    # place on board (runtime import to avoid cycles)
    from .generator import Key, Treasure

    board.features.setdefault(key_pos, []).append(Key())
    board.features.setdefault(treasure_pos, []).append(Treasure())

    return PlacementResult(key_pos=key_pos, treasure_pos=treasure_pos)
