"""Procedural generator for the "Labyrinth" paper game.

This file starts as a *skeleton* with the right seams:
- data model (grid + walls + entities)
- extensible rule system for new cell/object types
- generator pipeline
- solvability + difficulty evaluation

We will refine after we clarify a few rules (see Questions section).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Dict, Iterable, List, Optional, Protocol, Tuple, FrozenSet

from .config import GameConfig
from .rng import RNGStreams
from .rivers_gen import generate_linear_rivers
from .pits_gen import place_pit_cycles
from .grenades_gen import seal_cell_and_place_grenades
from .walls_gen import generate_walls_degree_v1
from .walls_river_gen import generate_river_adjacent_walls_v1
from .walls_engine_gen import generate_nonriver_walls_engine_v1
from .ergodic import check_no_sinks_from_start
from .placement2 import place_objects
from .exit_placement import apply_exit_edge, choose_exit_edge


# ----------------------------
# Core coordinates / directions

Coord = Tuple[int, int]  # (row, col)


class Dir(Enum):
    UP = (-1, 0)
    DOWN = (1, 0)
    LEFT = (0, -1)
    RIGHT = (0, 1)


def add(a: Coord, b: Coord) -> Coord:
    return (a[0] + b[0], a[1] + b[1])


# ----------------------------
# Board representation


@dataclass(frozen=True)
class WallSet:
    """Walls between orthogonally adjacent cells.

    Convention: we store blocked directed edges.
    If (u->v) is blocked then (v->u) should also be blocked.
    """

    blocked: frozenset[Tuple[Coord, Coord]] = frozenset()

    def is_blocked(self, u: Coord, v: Coord) -> bool:
        return (u, v) in self.blocked


@dataclass
class Board:
    n: int
    m: int
    exit_edge: Tuple[Coord, Dir]  # (border cell, outward direction) that is open to "outside"
    walls: WallSet = field(default_factory=WallSet)

    # cell annotations: multiple features can exist on same cell
    features: Dict[Coord, List["Feature"]] = field(default_factory=dict)

    def in_bounds(self, c: Coord) -> bool:
        return 0 <= c[0] < self.n and 0 <= c[1] < self.m

    def neighbors4(self, c: Coord) -> Iterable[Coord]:
        for d in Dir:
            nc = add(c, d.value)
            if self.in_bounds(nc):
                yield nc


# ----------------------------
# Extensible feature/rule system


class StepResult(Enum):
    OK = "ok"  # normal move resolved
    BLOCKED = "blocked"  # attempted move blocked by wall/monolith


@dataclass(frozen=True)
class PlayerState:
    pos: Coord
    has_treasure: bool = False
    has_key: bool = False
    has_exit_key: bool = False
    has_boat: bool = False
    grenades: int = 0
    flashlight: int = 0  # remaining flashlight charges for look_* actions
    # blown walls are stored as undirected edges (u,v) with u<=v in lexicographic order
    blown: FrozenSet[Tuple[Coord, Coord]] = frozenset()

    def copy(self, **changes):
        """Return a new PlayerState with specified fields changed."""
        return replace(self, **changes)


@dataclass(frozen=True)
class ForcedEvent:
    name: str
    from_pos: Coord
    to_pos: Coord
    steps: Optional[int] = None


@dataclass(frozen=True)
class Transition:
    """Outcome of a player action after all forced effects resolve.

    landed_pos is the position right after the player's intended movement succeeds
    (before forced displacements like rivers/pits). This is important for narration
    and for partial-observability UIs.
    """

    state: PlayerState
    landed_pos: Coord
    forced: Tuple[ForcedEvent, ...] = ()
    log: Tuple[str, ...] = ()  # human-readable explanations


class Feature(Protocol):
    """A board element that can affect movement/interaction.

    We model gameplay as a state machine:
    - player chooses an action (move dir)
    - engine applies base movement if not blocked
    - then resolves forced effects until stable (river push, pit teleport, etc.)

    Generator uses Feature types too (to place/configure them).
    """

    name: str

    def on_enter(self, board: Board, s: PlayerState) -> PlayerState:
        """Passive pickup/trigger when player is on the cell after movement.

        Must be pure (no mutation), return updated PlayerState.
        """

    def forced_move(self, board: Board, s: PlayerState) -> Optional[PlayerState]:
        """If the feature forces displacement, return the next PlayerState.

        Return None if no forced movement.
        """


# ----------------------------
# Concrete features (initial)


@dataclass(frozen=True)
class Key:
    name: str = "key"

    def on_enter(self, board: Board, s: PlayerState) -> PlayerState:
        if s.has_key:
            return s
        return PlayerState(
            pos=s.pos,
            has_treasure=s.has_treasure,
            has_key=True,
            has_exit_key=s.has_exit_key,
            has_boat=s.has_boat,
            grenades=s.grenades,
            blown=s.blown,
        )

    def forced_move(self, board: Board, s: PlayerState) -> Optional[PlayerState]:
        return None


@dataclass(frozen=True)
class Treasure:
    name: str = "treasure"

    def on_enter(self, board: Board, s: PlayerState) -> PlayerState:
        # Treasure is assumed to be in a chest that requires the (main) key.
        if s.has_treasure:
            return s
        if not s.has_key:
            return s
        return PlayerState(
            pos=s.pos,
            has_treasure=True,
            has_key=s.has_key,
            has_exit_key=s.has_exit_key,
            has_boat=s.has_boat,
            grenades=s.grenades,
            blown=s.blown,
        )

    def forced_move(self, board: Board, s: PlayerState) -> Optional[PlayerState]:
        return None


@dataclass(frozen=True)
class ExitKey:
    name: str = "exit_key"

    def on_enter(self, board: Board, s: PlayerState) -> PlayerState:
        if s.has_exit_key:
            return s
        return PlayerState(
            pos=s.pos,
            has_treasure=s.has_treasure,
            has_key=s.has_key,
            has_exit_key=True,
            has_boat=s.has_boat,
            grenades=s.grenades,
            blown=s.blown,
        )

    def forced_move(self, board: Board, s: PlayerState) -> Optional[PlayerState]:
        return None


# River/pit will be added after we lock down exact rules.


# ----------------------------
# Engine-like transition helpers (used by solver)


def apply_on_enter(board: Board, s: PlayerState) -> PlayerState:
    feats = board.features.get(s.pos, [])
    for f in feats:
        s = f.on_enter(board, s)
    return s


def resolve_forced(board: Board, s: PlayerState, max_chain: int = 100, *, landed_pos: Optional[Coord] = None) -> Transition:
    """Resolve forced movements until stable.

    Important: some forced systems (like rivers) should apply at most once per player action,
    even if the landing cell is also a river cell. We implement this by allowing each
    feature "name" to force-move at most once during this resolution.
    """

    log: List[str] = []
    forced: List[ForcedEvent] = []
    applied_force: set[str] = set()

    s = apply_on_enter(board, s)
    if landed_pos is None:
        landed_pos = s.pos

    for _ in range(max_chain):
        moved = False
        for f in board.features.get(s.pos, []):
            fname = getattr(f, "name", type(f).__name__)
            if fname in applied_force:
                continue

            steps = None
            if hasattr(f, "carry_steps"):
                try:
                    steps = int(getattr(f, "carry_steps")(board, s))
                except Exception:
                    steps = None

            ns = f.forced_move(board, s)
            if ns is not None and ns.pos != s.pos:
                applied_force.add(fname)
                forced.append(ForcedEvent(name=fname, from_pos=s.pos, to_pos=ns.pos, steps=steps))
                s = apply_on_enter(board, ns)
                log.append(f"forced by {fname} -> {s.pos}")
                moved = True
                break
        if not moved:
            return Transition(state=s, landed_pos=landed_pos, forced=tuple(forced), log=tuple(log))

    return Transition(state=s, landed_pos=landed_pos, forced=tuple(forced), log=tuple(log) + ("forced-move chain limit reached",))


def attempt_move(board: Board, s: PlayerState, d: Dir) -> Tuple[StepResult, Transition]:
    target = add(s.pos, d.value)

    # leaving the board: only allowed via the single open exit edge
    if not board.in_bounds(target):
        (er, ec), edir = board.exit_edge
        if s.pos == (er, ec) and d == edir:
            # represent "outside" as staying on the exit cell; engine will decide what to do
            return StepResult.OK, Transition(state=s, landed_pos=s.pos, forced=(), log=("exit edge",))
        return StepResult.BLOCKED, Transition(state=s, landed_pos=s.pos, forced=(), log=("monolith blocks movement",))

    # normal walls, unless blown in this state
    edge = (s.pos, target) if s.pos <= target else (target, s.pos)
    if edge not in s.blown and board.walls.is_blocked(s.pos, target):
        return StepResult.BLOCKED, Transition(state=s, landed_pos=s.pos, forced=(), log=("wall blocks movement",))

    ns = PlayerState(
        pos=target,
        has_treasure=s.has_treasure,
        has_key=s.has_key,
        has_exit_key=s.has_exit_key,
        has_boat=s.has_boat,
        grenades=s.grenades,
        flashlight=getattr(s, "flashlight", 0),
        blown=s.blown,
    )
    tr = resolve_forced(board, ns, landed_pos=target)
    return StepResult.OK, tr


# ----------------------------
# Solvability / difficulty


@dataclass(frozen=True)
class SolveGoal:
    """What counts as "win". We'll clarify with you and refine."""

    require_key_for_treasure: bool = False
    require_exit_key_to_leave: bool = False
    must_exit_after_treasure: bool = True


def is_win(board: Board, s: PlayerState, goal: SolveGoal) -> bool:
    # Placeholder: exiting condition depends on whether exiting is an action
    # or simply stepping onto exit_cell (and maybe having exit key).
    if goal.must_exit_after_treasure and not s.has_treasure:
        return False

    # For now treat "win" as: standing on the border cell that has the open exit edge.
    exit_cell, _ = board.exit_edge
    if s.pos != exit_cell:
        return False

    if goal.require_exit_key_to_leave and not s.has_exit_key:
        return False

    return True


@dataclass
class SolveStats:
    solvable: bool
    shortest_steps: Optional[int] = None
    explored_states: int = 0


def solve_bfs(board: Board, start: PlayerState, goal: SolveGoal, max_states: int = 200_000) -> SolveStats:
    """State-space BFS under full movement rules.

    Includes deterministic features (rivers/pits/pickups) and grenade wall-blasting.

    Note: unknown-map / partial observability is a *player difficulty* concept.
    For generator validation we use full-information solvability.
    """

    from collections import deque

    start_tr = resolve_forced(board, start, landed_pos=start.pos)
    start_state = start_tr.state

    q = deque([(start_state, 0)])
    seen = {start_state}

    def blast_neighbors(s: PlayerState) -> list[PlayerState]:
        """Generate states reachable by blasting exactly one adjacent wall."""
        if s.grenades <= 0:
            return []
        out: list[PlayerState] = []
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
            out.append(
                PlayerState(
                    pos=s.pos,
                    has_treasure=s.has_treasure,
                    has_key=s.has_key,
                    has_exit_key=s.has_exit_key,
                    grenades=s.grenades - 1,
                    blown=frozenset(blown),
                )
            )
        return out

    while q and len(seen) <= max_states:
        s, dist = q.popleft()

        if is_win(board, s, goal):
            return SolveStats(solvable=True, shortest_steps=dist, explored_states=len(seen))

        # blasting (counts as an action)
        for ns in blast_neighbors(s):
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


# ----------------------------
# Generator API (skeleton)


@dataclass
class Generated:
    config: GameConfig
    board: Board
    start: PlayerState
    goal: SolveGoal
    stats: SolveStats


def generate(cfg: GameConfig) -> Generated:
    """Generate a board according to config.

    Reproducibility notes:
    - We use per-subsystem RNG streams derived from cfg.rng.seed.
    - Disabled systems must not consume RNG, so enabling new systems later
      won't perturb old outputs.
    """

    streams = RNGStreams(cfg.rng.seed)

    # RNG streams
    rng_layout = streams.stream("layout")
    rng_items = streams.stream("items")
    rng_rivers = streams.stream("rivers")
    rng_walls = streams.stream("walls")

    n, m = cfg.board.n, cfg.board.m

    all_cells = [(r, c) for r in range(n) for c in range(m)]

    # Create board with temporary exit; will choose real exit AFTER placing rivers.
    board = Board(n=n, m=m, exit_edge=((0, 0), Dir.UP))

    # rivers first (rivers stream)
    if cfg.systems.rivers.enabled:
        if cfg.systems.rivers.strategy != "linear-v1":
            raise ValueError(f"Unknown river strategy: {cfg.systems.rivers.strategy}")

        # Enforce minimum river coverage: if underfilled (due to dead-ends), retry whole river layout.
        area = n * m
        dens = float(cfg.systems.rivers.density)
        scat = float(getattr(cfg.systems.rivers, "scatter", 0.0))
        min_river_cells = int((area * dens) * (1.0 - max(0.0, scat)))
        if min_river_cells < 3:
            min_river_cells = 3

        ok_rivers = False
        for attempt in range(30):
            board.features = {}
            rng_try = RNGStreams(cfg.rng.seed).stream(f"rivers:{attempt}")
            generate_linear_rivers(
                board,
                cfg=cfg.systems.rivers,
                rng=rng_try,
                forbid_cells=set(),
                force_source_on_border=bool(cfg.systems.boats.enabled),
            )

            river_cells_tmp = {pos for pos, feats in board.features.items() for f in feats if getattr(f, "name", "") == "river"}
            if len(river_cells_tmp) >= min_river_cells:
                ok_rivers = True
                break

        if not ok_rivers:
            # keep last attempt (still reproducible); later we can add a repair/relaxation mechanism
            pass

    # compute river-related sets
    river_cells = {pos for pos, feats in board.features.items() for f in feats if getattr(f, "name", "") == "river"}
    river_sources: set[Coord] = set()
    river_after_source: set[Coord] = set()
    for pos, feats in board.features.items():
        for f in feats:
            if getattr(f, "name", "") == "river" and getattr(f, "kind", None) == "source":
                river_sources.add(pos)
                flow = getattr(f, "flow", None)
                if flow is not None:
                    nxt = add(pos, flow.value)
                    if 0 <= nxt[0] < n and 0 <= nxt[1] < m:
                        river_after_source.add(nxt)

    # temporarily keep exit edge closed; we'll place the exit AFTER objects using placement.
    exit_cell = (0, 0)
    board.exit_edge = (exit_cell, Dir.UP)

    # choose start (layout stream): start must NOT be in river
    start_candidates = [c for c in all_cells if c not in river_cells]
    if not start_candidates:
        start_candidates = all_cells
    start_pos = rng_layout.choice(start_candidates)
    start = PlayerState(pos=start_pos)

    # walls (walls stream)
    if cfg.systems.walls.enabled:
        if cfg.systems.walls.strategy != "degree-v1":
            raise ValueError(f"Unknown wall strategy: {cfg.systems.walls.strategy}")

        # Stage-1: only between non-river cells.
        # Try multiple wall layouts until we avoid sink-reachable behavior from start
        # under the full movement rules (incl. rivers).
        from .generator import WallSet

        ok = False
        for attempt in range(30):
            board.walls = WallSet(frozenset())
            rng_try = RNGStreams(cfg.rng.seed).stream(f"walls:{attempt}")

            generate_walls_degree_v1(
                board,
                rng=rng_try,
                target_mean_degree=float(cfg.systems.walls.target_mean_degree),
            )

            chk = check_no_sinks_from_start(board, start.pos)
            if chk.ok:
                ok = True
                break

        # Stage-1.5: add more walls between non-river cells, but allow connectivity via rivers.
        if ok and cfg.systems.walls.nonriver_engine_enabled and cfg.systems.rivers.enabled:
            rng_nonriver_eng = RNGStreams(cfg.rng.seed).stream("walls_nonriver_engine")
            generate_nonriver_walls_engine_v1(
                board,
                rng=rng_nonriver_eng,
                start=start.pos,
                target_mean_degree=float(cfg.systems.walls.target_mean_degree),
                global_check=check_no_sinks_from_start,
                global_check_period=10,
            )

        # Stage-2: walls on river boundaries (river<->nonriver).
        if ok and cfg.systems.walls.river_enabled and cfg.systems.rivers.enabled:
            rng_river_walls = RNGStreams(cfg.rng.seed).stream("walls_river")
            generate_river_adjacent_walls_v1(
                board,
                rng=rng_river_walls,
                start=start.pos,
                target_mean_river_walls=float(cfg.systems.walls.target_mean_river_walls),
                global_check=check_no_sinks_from_start,
                global_check_period=10,
            )

        # If we failed to find a sink-free walling quickly, keep the last attempt;
        # later stages can still repair by opening walls.

    # pits (dynamic system): place after walls (so we can enforce "not 4-walls"),
    # but before key objects (so placement/solvability accounts for pit dynamics).
    if cfg.systems.pits_cfg.enabled:
        rng_pits = streams.stream("pits")
        # Do not place pits on the start cell (paper-UI friendliness; avoids hiding under 'S').
        place_pit_cycles(board, rng=rng_pits, cfg=cfg.systems.pits_cfg, forbid={start.pos})

    # place items (items stream)
    if cfg.systems.keys or cfg.systems.treasure or cfg.systems.exit_key or cfg.systems.boats.enabled:
        order = []
        if cfg.systems.keys:
            order.append("key")
        if cfg.systems.treasure:
            order.append("treasure")
        if cfg.systems.exit_key:
            order.append("exit_key")
        if cfg.systems.boats.enabled:
            order.append("boat")

        from .placement2 import PlacementPlan

        place_objects(
            board,
            start=start.pos,
            exit_cell=exit_cell,
            rng=rng_items,
            plan=PlacementPlan(order=order),
            walks_per_anchor=20,
            penalty_walk_len=(n + m),
        )

    # place exit after objects: choose a rare border cell; if corner, random outward edge.
    # Avoid placing exit on (or right next to) other key objects.
    key_cells: set[Coord] = set()
    for pos, feats in board.features.items():
        for f in feats:
            if getattr(f, "name", "") in ("key", "treasure", "exit_key", "boat"):
                key_cells.add(pos)

    key_and_neighbors: set[Coord] = set(key_cells)
    for (r, c) in list(key_cells):
        for dr, dc in [(-1,0),(1,0),(0,-1),(0,1)]:
            nb = (r+dr, c+dc)
            if 0 <= nb[0] < n and 0 <= nb[1] < m:
                key_and_neighbors.add(nb)

    if cfg.systems.rivers.enabled:
        forbidden_exit = set(river_sources) | set(river_after_source) | {start.pos} | key_and_neighbors
    else:
        forbidden_exit = {start.pos} | key_and_neighbors

    rng_exit = RNGStreams(cfg.rng.seed).stream("exit")
    choice = choose_exit_edge(board, start=start.pos, rng=rng_exit, forbidden=forbidden_exit)
    apply_exit_edge(board, choice)
    exit_cell = choice.cell

    # boats: river-exit-v1 heuristic
    # If enabled, force at least one river source to be adjacent to the monolith and place the exit there.
    if cfg.systems.boats.enabled:
        if cfg.systems.boats.strategy != "river-exit-v1":
            raise ValueError(f"Unknown boats strategy: {cfg.systems.boats.strategy}")

        river_sources_now: list[Coord] = []
        for pos, feats in board.features.items():
            for f in feats:
                if getattr(f, "name", "") == "river" and getattr(f, "kind", None) == "source":
                    river_sources_now.append(pos)
                    break

        def touches_monolith(c: Coord) -> bool:
            r, cc = c
            return r == 0 or r == n - 1 or cc == 0 or cc == m - 1

        cand = [c for c in river_sources_now if touches_monolith(c)]
        if cand:
            # choose an outward direction (deterministic preference: if on top edge -> UP, bottom -> DOWN, left -> LEFT, right -> RIGHT)
            cell = sorted(cand)[0]
            r0, c0 = cell
            if r0 == 0:
                edir = Dir.UP
            elif r0 == n - 1:
                edir = Dir.DOWN
            elif c0 == 0:
                edir = Dir.LEFT
            else:
                edir = Dir.RIGHT

            # overwrite exit edge to this river source (keeping it as the single open border edge)
            board.exit_edge = (cell, edir)
            exit_cell = cell

    # exit_key placement is handled together with other key objects in placement2.place_objects

    goal = SolveGoal(
        require_key_for_treasure=False,
        require_exit_key_to_leave=cfg.systems.exit_key,
        must_exit_after_treasure=True,
    )

    # First, compute baseline solvability BEFORE grenade sealing (fast and stable).
    base_stats = solve_bfs(board, start, goal)

    # grenades: seal-cell-v1 + place grenade pack AFTER sealing (so it can't get sealed too)
    seal_res = None
    if cfg.systems.grenades.enabled:
        rng_gren = streams.stream("grenades")
        seal_res = seal_cell_and_place_grenades(
            board,
            rng=rng_gren,
            extra_grenades=int(cfg.systems.grenades.extra_grenades),
            max_walls_to_add=int(cfg.systems.grenades.max_walls_to_add),
            forbid_seal={start.pos, exit_cell},
            forbid_pack={start.pos, exit_cell},
            start=start.pos,
        )

        # Fast reachability sanity checks (no blown-state BFS)
        if seal_res.sealed_cell is not None and seal_res.pack_pos is not None:
            from .grenade_checks import check_seal_cell_v1

            chk = check_seal_cell_v1(
                board,
                start=start,
                sealed_cell=seal_res.sealed_cell,
                pack_pos=seal_res.pack_pos,
                max_states=200_000,
            )

            # Report solvability in grenade-enabled mode using the fast sufficient check.
            stats = SolveStats(
                solvable=chk.ok,
                shortest_steps=base_stats.shortest_steps,
                explored_states=base_stats.explored_states,
            )
        else:
            stats = SolveStats(solvable=False, shortest_steps=base_stats.shortest_steps, explored_states=base_stats.explored_states)
    else:
        stats = base_stats

    return Generated(config=cfg, board=board, start=start, goal=goal, stats=stats)


# ----------------------------
# Questions (to resolve before implementing river/pits/walls fully)

# 1) Win condition:
#    - Is the goal "find treasure" only, or "treasure + exit"?
#    - Is stepping onto exit_cell enough, or does exiting require an action?
#
# 2) Keys:
#    - Does key gate treasure pickup, or gates opening some door, or both?
#
# 3) Rivers:
#    - Representation: is river a path (sequence of cells) with unique successor (flow)?
#    - On entering any river cell, player is moved 2 cells along flow.
#      If next is mouth, moved 1. What if river length < 2 to mouth?
#      What if river branches/merges? (assume no branches?)
#    - Can river cells have walls on their edges? Does current ignore walls?
#
# 4) Pits (teleports):
#    - Trigger on enter? forced, always? Any limit?
#    - Can pits coexist with river on same cell? If yes, priority order?
#
# 5) Difficulty notion:
#    - For generator, we can target full-information shortest path length.
#      For "human" difficulty we may need additional metrics (ambiguity / partial observability).
