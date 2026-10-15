"""Solver helpers for grenade-enabled reachability.

We provide a dedicated solver for the generator that can handle a small number
of grenade blasts by including (grenades, blown_edges) in the state.

This keeps the original solve_bfs() lightweight for non-grenade games.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional, Set, Tuple

from .generator import Board, Dir, PlayerState, StepResult, SolveGoal, SolveStats, add, attempt_move, is_win, resolve_forced


Coord = Tuple[int, int]


def solve_bfs_with_grenades(
    board: Board,
    start: PlayerState,
    goal: SolveGoal,
    *,
    max_states: int = 250_000,
) -> SolveStats:
    start_tr = resolve_forced(board, start, landed_pos=start.pos)
    s0 = start_tr.state

    q = deque([(s0, 0)])
    seen: Set[PlayerState] = {s0}

    while q and len(seen) <= max_states:
        s, dist = q.popleft()

        if is_win(board, s, goal):
            return SolveStats(solvable=True, shortest_steps=dist, explored_states=len(seen))

        # blasting (counts as an action)
        if s.grenades > 0:
            for d in Dir:
                nb = add(s.pos, d.value)
                if not board.in_bounds(nb):
                    continue  # monolith not blastable
                if not board.walls.is_blocked(s.pos, nb):
                    continue
                e = (s.pos, nb) if s.pos <= nb else (nb, s.pos)
                if e in s.blown:
                    continue
                blown = set(s.blown)
                blown.add(e)
                ns = PlayerState(
                    pos=s.pos,
                    has_treasure=s.has_treasure,
                    has_key=s.has_key,
                    has_exit_key=s.has_exit_key,
                    has_boat=s.has_boat,
                    grenades=s.grenades - 1,
                    blown=frozenset(blown),
                )
                if ns not in seen:
                    seen.add(ns)
                    q.append((ns, dist + 1))

        # movement
        for d in Dir:
            res, tr = attempt_move(board, s, d)
            if res is StepResult.BLOCKED:
                continue
            ns = tr.state
            if ns not in seen:
                seen.add(ns)
                q.append((ns, dist + 1))

    return SolveStats(solvable=False, shortest_steps=None, explored_states=len(seen))
