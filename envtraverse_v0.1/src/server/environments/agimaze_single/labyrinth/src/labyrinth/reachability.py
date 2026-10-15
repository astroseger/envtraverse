"""Reachability utilities (full movement rules, but no item-grabbing goals).

We need this for generation heuristics (e.g., placing grenade packs so they are
reachable without using grenades).

We treat the state space as PlayerState with inventory fields, but we only care
about which positions are reachable.

Important:
- We do NOT allow blasting here; grenades are ignored.
- We DO resolve forced effects (rivers/pits).
"""

from __future__ import annotations

from collections import deque
from typing import Set, Tuple

from .generator import Board, Dir, PlayerState, StepResult, attempt_move, resolve_forced


Coord = Tuple[int, int]


def reachable_positions(board: Board, start: PlayerState, *, max_states: int = 200_000) -> Set[Coord]:
    start_tr = resolve_forced(board, start, landed_pos=start.pos)
    s0 = start_tr.state

    q = deque([s0])
    seen = {s0}
    pos_seen: Set[Coord] = {s0.pos}

    while q and len(seen) <= max_states:
        s = q.popleft()
        for d in Dir:
            res, tr = attempt_move(board, s, d)
            if res is StepResult.BLOCKED:
                continue
            ns = tr.state
            if ns not in seen:
                seen.add(ns)
                pos_seen.add(ns.pos)
                q.append(ns)

    return pos_seen
