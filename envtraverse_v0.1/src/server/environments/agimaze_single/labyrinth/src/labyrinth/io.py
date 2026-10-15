"""Save/load generated labyrinths.

We support a "bundle" JSON format that contains:
- the generation config (as JSON-able dict)
- optionally a pre-generated board/start/goal snapshot

This is intended for:
- benchmark packs (fast loading, no generation latency)
- regression tests (ensure generation hasn't changed)

Format (bundle):
{
  "format": "labyrinth.bundle.v1",
  "config": { ... },
  "generated": {
     "board": { ... },
     "start": { ... },
     "goal": { ... }
  }
}

Notes:
- We store enough to recreate Board/PlayerState/SolveGoal.
- We do NOT store solver stats (can be recomputed).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List, Tuple

from .config import GameConfig
from .generator import Board, Dir, PlayerState, SolveGoal, WallSet


Coord = Tuple[int, int]


def _dir_to_name(d: Dir | None) -> str | None:
    if d is None:
        return None
    return d.name.lower()


def _dir_from_name(name: str | None) -> Dir | None:
    if name is None:
        return None
    mp = {"up": Dir.UP, "down": Dir.DOWN, "left": Dir.LEFT, "right": Dir.RIGHT}
    return mp[name]


def board_to_dict(board: Board) -> Dict[str, Any]:
    # walls: list of directed blocked edges [[u],[v]]
    walls = [([u[0], u[1]], [v[0], v[1]]) for (u, v) in sorted(board.walls.blocked)]

    # exit edge
    (er, ec), edir = board.exit_edge

    # features
    feats_out: Dict[str, List[Dict[str, Any]]] = {}
    for pos, feats in board.features.items():
        key = f"{pos[0]},{pos[1]}"
        lst = []
        for f in feats:
            nm = getattr(f, "name", type(f).__name__)
            if nm == "river":
                lst.append(
                    {
                        "name": "river",
                        "kind": getattr(f, "kind", "body"),
                        "flow": _dir_to_name(getattr(f, "flow", None)),
                    }
                )
            elif nm == "pit":
                nxt = getattr(f, "next_pos")
                lst.append(
                    {
                        "name": "pit",
                        "next": [int(nxt[0]), int(nxt[1])],
                        "cycle_id": int(getattr(f, "cycle_id", 0)),
                        "idx_in_cycle": int(getattr(f, "idx_in_cycle", 0)),
                    }
                )
            elif nm == "grenade_pack":
                lst.append({"name": "grenade_pack", "count": int(getattr(f, "count", 0))})
            elif nm in ("key", "treasure", "exit_key", "boat"):
                lst.append({"name": nm})
            else:
                # Unknown feature: best-effort store only name.
                lst.append({"name": nm})
        feats_out[key] = lst

    return {
        "n": int(board.n),
        "m": int(board.m),
        "exit_edge": {"cell": [int(er), int(ec)], "dir": _dir_to_name(edir)},
        "walls": walls,
        "features": feats_out,
    }


def board_from_dict(d: Dict[str, Any]) -> Board:
    n = int(d["n"])
    m = int(d["m"])

    exit_cell = tuple(d["exit_edge"]["cell"])  # type: ignore[assignment]
    exit_dir = _dir_from_name(d["exit_edge"]["dir"])
    if exit_dir is None:
        raise ValueError("exit_edge.dir must be set")

    board = Board(n=n, m=m, exit_edge=(exit_cell, exit_dir))

    # walls
    blocked = set()
    for u, v in d.get("walls", []):
        uu = (int(u[0]), int(u[1]))
        vv = (int(v[0]), int(v[1]))
        blocked.add((uu, vv))
    board.walls = WallSet(frozenset(blocked))

    # features
    from .rivers import RiverCell
    from .pits import Pit
    from .generator import Key, Treasure, ExitKey
    from .boats import Boat
    from .grenades import GrenadePack

    feats_in: Dict[str, List[Dict[str, Any]]] = d.get("features", {})
    for k, lst in feats_in.items():
        r_s, c_s = k.split(",")
        pos = (int(r_s), int(c_s))
        for fd in lst:
            nm = fd.get("name")
            if nm == "river":
                flow = _dir_from_name(fd.get("flow"))
                board.features.setdefault(pos, []).append(RiverCell(kind=str(fd.get("kind", "body")), flow=flow))
            elif nm == "pit":
                nxt = fd.get("next")
                board.features.setdefault(pos, []).append(
                    Pit(
                        next_pos=(int(nxt[0]), int(nxt[1])),
                        cycle_id=int(fd.get("cycle_id", 0)),
                        idx_in_cycle=int(fd.get("idx_in_cycle", 0)),
                    )
                )
            elif nm == "key":
                board.features.setdefault(pos, []).append(Key())
            elif nm == "treasure":
                board.features.setdefault(pos, []).append(Treasure())
            elif nm == "exit_key":
                board.features.setdefault(pos, []).append(ExitKey())
            elif nm == "boat":
                board.features.setdefault(pos, []).append(Boat())
            elif nm == "grenade_pack":
                board.features.setdefault(pos, []).append(GrenadePack(count=int(fd.get("count", 0))))
            else:
                # Unknown feature is ignored on load (no behavior).
                pass

    return board


def player_state_to_dict(s: PlayerState) -> Dict[str, Any]:
    return {
        "pos": [int(s.pos[0]), int(s.pos[1])],
        "has_treasure": bool(s.has_treasure),
        "has_key": bool(s.has_key),
        "has_exit_key": bool(s.has_exit_key),
        "has_boat": bool(s.has_boat),
        "grenades": int(s.grenades),
        "flashlight": int(getattr(s, "flashlight", 0)),
        "blown": [([u[0], u[1]], [v[0], v[1]]) for (u, v) in sorted(s.blown)],
    }


def player_state_from_dict(d: Dict[str, Any]) -> PlayerState:
    pos = tuple(d["pos"])  # type: ignore[assignment]
    blown = set()
    for u, v in d.get("blown", []):
        uu = (int(u[0]), int(u[1]))
        vv = (int(v[0]), int(v[1]))
        blown.add((uu, vv) if uu <= vv else (vv, uu))

    return PlayerState(
        pos=(int(pos[0]), int(pos[1])),
        has_treasure=bool(d.get("has_treasure", False)),
        has_key=bool(d.get("has_key", False)),
        has_exit_key=bool(d.get("has_exit_key", False)),
        has_boat=bool(d.get("has_boat", False)),
        grenades=int(d.get("grenades", 0)),
        flashlight=int(d.get("flashlight", 0)),
        blown=frozenset(blown),
    )


def goal_to_dict(g: SolveGoal) -> Dict[str, Any]:
    return {
        "require_key_for_treasure": bool(g.require_key_for_treasure),
        "require_exit_key_to_leave": bool(g.require_exit_key_to_leave),
        "must_exit_after_treasure": bool(g.must_exit_after_treasure),
    }


def goal_from_dict(d: Dict[str, Any]) -> SolveGoal:
    return SolveGoal(
        require_key_for_treasure=bool(d.get("require_key_for_treasure", False)),
        require_exit_key_to_leave=bool(d.get("require_exit_key_to_leave", False)),
        must_exit_after_treasure=bool(d.get("must_exit_after_treasure", True)),
    )


def bundle_to_dict(*, config: Dict[str, Any], board: Board, start: PlayerState, goal: SolveGoal) -> Dict[str, Any]:
    return {
        "format": "labyrinth.bundle.v1",
        "config": config,
        "generated": {
            "board": board_to_dict(board),
            "start": player_state_to_dict(start),
            "goal": goal_to_dict(goal),
        },
    }


def bundle_has_generated(bundle: Dict[str, Any]) -> bool:
    return isinstance(bundle.get("generated"), dict)


def load_bundle(bundle: Dict[str, Any]) -> tuple[Dict[str, Any], Board, PlayerState, SolveGoal]:
    if bundle.get("format") != "labyrinth.bundle.v1":
        raise ValueError("Unsupported bundle format")
    cfg = dict(bundle.get("config") or {})
    gen = bundle.get("generated")
    if not isinstance(gen, dict):
        raise ValueError("Bundle has no generated section")

    board = board_from_dict(gen["board"])
    start = player_state_from_dict(gen["start"])
    goal = goal_from_dict(gen["goal"])
    return cfg, board, start, goal
