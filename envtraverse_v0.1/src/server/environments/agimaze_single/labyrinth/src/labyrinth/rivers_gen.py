"""River generation strategies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from .config import RiversConfig
from .rivers import RiverCell

# To avoid circular imports, we use structural typing for Board and re-define minimal helpers.
from typing import Protocol

class BoardLike(Protocol):
    n: int
    m: int
    features: dict
    def in_bounds(self, c: tuple[int,int]) -> bool: ...

Coord = tuple[int, int]

class DirEnum(Protocol):
    value: tuple[int,int]

# We'll import Dir at runtime in the function (generator defines it).

def add(a: Coord, b: tuple[int,int]) -> Coord:
    return (a[0] + b[0], a[1] + b[1])


@dataclass
class RiverGenResult:
    placed_cells: int
    num_rivers: int


def generate_linear_rivers(
    board: BoardLike,
    *,
    cfg: RiversConfig,
    rng,
    forbid_cells: set[Coord],
    force_source_on_border: bool = False,
) -> RiverGenResult:
    """Place a set of non-branching rivers as self-avoiding random walks.

    - Rivers do not overlap (no cell can belong to 2 rivers).
    - Adjacency is allowed if cfg.allow_adjacent=True.
    """

    n, m = board.n, board.m
    area = n * m

    # First pick a target total river area (cells), with scatter.
    base_area = float(area) * float(cfg.density)
    scatter = max(0.0, float(getattr(cfg, "scatter", 0.0)))
    if scatter > 0:
        base_area *= rng.uniform(1.0 - scatter, 1.0 + scatter)
    area_target = max(3.0, base_area)

    # Preliminary effective length scale from mean_len (relative to (n+m)/2).
    eff_len0 = max(3, int(round(((n + m) / 2.0) * cfg.mean_len)))

    # Choose number of rivers from target area and preliminary length scale.
    k = max(1, int(round(area_target / eff_len0)))

    # Recompute effective length so that density is more "defining": k * eff_len ~= area_target.
    eff_len = max(3, int(area_target // k))

    used: set[Coord] = set()

    def ok_cell(c: Coord) -> bool:
        if c in forbid_cells:
            return False
        if c in used:
            return False
        return True

    # import Dir lazily to avoid circular import at module import time
    from .generator import Dir

    def candidates_next(c: Coord) -> List[Tuple[Coord, Dir]]:
        out: List[Tuple[Coord, Dir]] = []
        for d in Dir:
            nc = add(c, d.value)
            if not board.in_bounds(nc):
                continue
            if nc in used:
                continue
            if nc in forbid_cells:
                continue
            out.append((nc, d))
        return out

    def river_neighbor_count(c: Coord, *, river_set: set[Coord]) -> int:
        """Effective neighbor count for adjacency penalty.

        Counts:
        - 4-neighbors that are already river (including current path)
        - monolith border around the board as if it were a river neighbor (to discourage hugging the border)
        """
        r, c0 = c
        cnt = 0
        for d in Dir:
            nc = add(c, d.value)
            if nc in river_set:
                cnt += 1
            # monolith as penalty neighbor
            if not board.in_bounds(nc):
                cnt += 1
        return cnt

    def weighted_choice(options: List[Tuple[Coord, Dir, float]]):
        total = sum(w for _, _, w in options)
        if total <= 0:
            # fallback uniform
            return options[rng.randrange(len(options))][:2]
        x = rng.random() * total
        acc = 0.0
        for nc, d, w in options:
            acc += w
            if x <= acc:
                return nc, d
        return options[-1][:2]

    placed_cells = 0
    rivers_done = 0

    # If we underfill river area due to early dead-ends, we will keep trying to add rivers
    # until we reach at least the lower bound of the target area.
    min_area = int(round(area_target * (1.0 - scatter))) if scatter > 0 else int(round(area_target))
    if min_area < 3:
        min_area = 3

    def dist_map_to_used() -> list[list[int]]:
        """BFS distance to nearest already-placed river cell (4-neighborhood).

        Monolith is NOT included here (per spec).
        """
        from collections import deque

        INF = 10**9
        dist = [[INF] * m for _ in range(n)]
        q = deque()
        for (rr, cc) in used:
            dist[rr][cc] = 0
            q.append((rr, cc))

        # If no rivers yet, keep INF and treat as large distance later.
        while q:
            rr, cc = q.popleft()
            for d in Dir:
                nr, nc = rr + d.value[0], cc + d.value[1]
                if 0 <= nr < n and 0 <= nc < m:
                    if dist[nr][nc] > dist[rr][cc] + 1:
                        dist[nr][nc] = dist[rr][cc] + 1
                        q.append((nr, nc))
        return dist

    def pick_source(dist) -> Coord:
        # sample multiple candidates and choose weighted among them
        samples = 60
        opts = []
        p = float(getattr(cfg, "source_dist_power", 0.0))

        def is_border(c: Coord) -> bool:
            return c[0] in (0, n - 1) or c[1] in (0, m - 1)

        for _ in range(samples):
            c = (rng.randrange(n), rng.randrange(m))
            if force_source_on_border and not is_border(c):
                continue
            if not ok_cell(c):
                continue
            d0 = dist[c[0]][c[1]]
            if d0 >= 10**8:
                d0 = (n + m)  # no rivers yet
            w = (1.0 + float(d0)) ** max(0.0, p)
            opts.append((c, w))

        if not opts:
            # fallback
            while True:
                c = (rng.randrange(n), rng.randrange(m))
                if force_source_on_border and not is_border(c):
                    continue
                if ok_cell(c):
                    return c

        total = sum(w for _, w in opts)
        x = rng.random() * total if total > 0 else 0
        acc = 0.0
        for c, w in opts:
            acc += w
            if x <= acc:
                return c
        return opts[-1][0]

    # Try multiple attempts; we may place more than k rivers if we underfill.
    attempts = 400 * max(1, k)
    while (rivers_done < k or placed_cells < min_area) and attempts > 0:
        attempts -= 1

        dist = dist_map_to_used()
        start = pick_source(dist)

        path: List[Coord] = [start]
        path_set = {start}
        dirs: List[Dir] = []

        # grow path (self-avoiding within the river)
        target_len = eff_len
        prev_dir: Dir | None = None

        # river_set used for adjacency penalty includes already-placed rivers and current path
        river_set = set(used) | set(path_set)

        for _ in range(target_len - 1):
            cur = path[-1]
            base_cands = [(nc, d) for (nc, d) in candidates_next(cur) if nc not in path_set]
            if not base_cands:
                break

            opts: List[Tuple[Coord, Dir, float]] = []
            for nc, d in base_cands:
                w = 1.0
                if prev_dir is not None and d == prev_dir:
                    w *= float(getattr(cfg, "straight_bias", 1.0))

                adj = river_neighbor_count(nc, river_set=river_set)
                pen = float(getattr(cfg, "adj_penalty", 1.0))
                if pen < 0.0:
                    pen = 0.0
                if pen > 1.0:
                    pen = 1.0
                # allow_adjacent controls hard ban only; otherwise penalty is soft
                if not cfg.allow_adjacent and adj > 0:
                    continue
                w *= (pen ** adj)

                opts.append((nc, d, w))

            if not opts:
                break

            nc, d = weighted_choice(opts)
            path.append(nc)
            path_set.add(nc)
            river_set.add(nc)
            dirs.append(d)
            prev_dir = d

        # discard too-short rivers
        if len(path) < 3:
            continue

        # place features: source/body have flow; mouth has no flow
        for i, cell in enumerate(path):
            used.add(cell)
            if i == len(path) - 1:
                board.features.setdefault(cell, []).append(RiverCell(kind="mouth", flow=None))
            else:
                kind = "source" if i == 0 else "body"
                board.features.setdefault(cell, []).append(RiverCell(kind=kind, flow=dirs[i]))

        placed_cells += len(path)
        rivers_done += 1

        # stop early if we already met the target area
        if placed_cells >= int(area_target) and rivers_done >= 1:
            break

    return RiverGenResult(placed_cells=placed_cells, num_rivers=rivers_done)
