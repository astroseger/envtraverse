"""Game engine (deterministic).

Design goal: a small, stable API that can be used by:
- humans (text interface)
- bots (JSON observations)
- the generator's solvability/difficulty checker

At this stage we support:
- 4-neighborhood movement with walls + monolith border
- exit is a *missing border wall* (Board.exit_edge)
- pickups: key/treasure/exit_key
- forced-move hooks (future: rivers, pits)

The engine is intentionally separated from the generator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Literal

from .generator import Board, Dir, PlayerState, StepResult, Transition, add, attempt_move, resolve_forced
from .i18n import Lang, DIR_WORD, cells_phrase, t


Action = Literal[
    "up",
    "down",
    "left",
    "right",
    "bomb_up",
    "bomb_down",
    "bomb_left",
    "bomb_right",
    "look_up",
    "look_down",
    "look_left",
    "look_right",
]


def available_actions(*, grenades_enabled: bool, flashlight_enabled: bool) -> tuple[str, ...]:
    base = ("up", "down", "left", "right")
    if flashlight_enabled:
        base = base + ("look_up", "look_down", "look_left", "look_right")
    if not grenades_enabled:
        return base
    return base + ("bomb_up", "bomb_down", "bomb_left", "bomb_right")


@dataclass(frozen=True)
class Observation:
    status: Literal["success", "blocked"]
    observe: Literal["empty", "key", "treasure", "exit_key", "boat", "grenade_pack", "exit"]
    pos: tuple[int, int]
    inventory: Dict[str, object]
    forced_chain: tuple[tuple[int, int], ...] = ()
    events: tuple[str, ...] = ()  # e.g. ("picked_key", "opened_treasure", "exit_blocked", "exited")
    details: Dict[str, object] | None = None  # extra structured info (no coordinates)


@dataclass(frozen=True)
class StepOutput:
    obs: Observation
    text: str
    state: PlayerState


def _observe_cell_from_features(features: dict[tuple[int, int], list[object]], pos: tuple[int, int]) -> str:
    feats = features.get(pos, [])
    names = {getattr(f, "name", type(f).__name__) for f in feats}
    if "treasure" in names:
        return "treasure"
    if "exit_key" in names:
        return "exit_key"
    if "key" in names:
        return "key"
    if "boat" in names:
        return "boat"
    if "grenade_pack" in names:
        return "grenade_pack"
    return "empty"


def _dir_from_action(action: Action) -> Dir:
    return {
        "up": Dir.UP,
        "down": Dir.DOWN,
        "left": Dir.LEFT,
        "right": Dir.RIGHT,
        "bomb_up": Dir.UP,
        "bomb_down": Dir.DOWN,
        "bomb_left": Dir.LEFT,
        "bomb_right": Dir.RIGHT,
        "look_up": Dir.UP,
        "look_down": Dir.DOWN,
        "look_left": Dir.LEFT,
        "look_right": Dir.RIGHT,
    }[action]


def _forced_chain_from_transition(tr: Transition, start_pos: tuple[int, int]) -> tuple[tuple[int, int], ...]:
    # We don't yet have structured forced info; we infer from log strings when present.
    # For now return empty unless forced moves happened.
    if not tr.log:
        return ()
    # We can't reliably parse positions from arbitrary strings; keep it empty for now.
    # Later we'll make forced moves structured.
    return ()


class Engine:
    def __init__(
        self,
        board: Board,
        start: PlayerState,
        *,
        reveal_consumed_locations: bool = True,
        lang: Lang = "ru",
        require_exit_key_to_leave: bool = False,
        grenades_enabled: bool = False,
        boats_enabled: bool = False,
        flashlight_enabled: bool = False,
        flashlight_charges: int = 0,
    ):
        self.board = board
        self.state = start
        self.lang: Lang = lang
        self.require_exit_key_to_leave = require_exit_key_to_leave
        self.grenades_enabled = grenades_enabled
        self.boats_enabled = boats_enabled
        self.flashlight_enabled = flashlight_enabled
        if self.flashlight_enabled and getattr(self.state, "flashlight", 0) <= 0:
            self.state = self.state.copy(flashlight=int(flashlight_charges))

        # Engine keeps its own mutable view of remaining features (so items can be consumed)
        self._features = {k: list(v) for k, v in board.features.items()}
        self._consumed: set[tuple[tuple[int, int], str]] = set()
        self.reveal_consumed_locations = reveal_consumed_locations

    def _inventory(self) -> Dict[str, object]:
        inv: Dict[str, object] = {
            "key": self.state.has_key,
            "treasure": self.state.has_treasure,
        }
        # Only expose exit_key when the rules require it (otherwise it's confusing noise).
        if self.require_exit_key_to_leave:
            inv["exit_key"] = self.state.has_exit_key
        if self.boats_enabled:
            inv["boat"] = self.state.has_boat
        if self.grenades_enabled:
            inv["grenades"] = self.state.grenades
        if self.flashlight_enabled:
            inv["flashlight"] = getattr(self.state, "flashlight", 0)
        return inv

    def step(self, action: Action) -> StepOutput:
        prev = self.state

        is_look = action.startswith("look_")
        if is_look:
            if not self.flashlight_enabled:
                obs = Observation(
                    status="blocked",
                    observe=_observe_cell_from_features(self._features, self.state.pos),
                    pos=self.state.pos,
                    inventory=self._inventory(),
                    forced_chain=(),
                    events=("flashlight_disabled",),
                )
                txt = {"ru": "Фонарик отключен.", "en": "Flashlight is disabled."}[self.lang]
                return StepOutput(obs=obs, text=txt, state=self.state)

            if getattr(self.state, "flashlight", 0) <= 0:
                obs = Observation(
                    status="blocked",
                    observe=_observe_cell_from_features(self._features, self.state.pos),
                    pos=self.state.pos,
                    inventory=self._inventory(),
                    forced_chain=(),
                    events=("no_flashlight_charges",),
                )
                txt = t("look.no_charges", self.lang)
                return StepOutput(obs=obs, text=txt, state=self.state)

            d = _dir_from_action(action)
            target = add(self.state.pos, d.value)

            # Determine what would happen if the player attempted to step there.
            if not self.board.in_bounds(target):
                what_key = "look.exit" if (self.state.pos == self.board.exit_edge[0] and d == self.board.exit_edge[1]) else "look.monolith"
                what = t(what_key, self.lang)
                status = "success" if what_key == "look.exit" else "blocked"
                details = {"dir": d.name.lower(), "kind": ("exit" if what_key == "look.exit" else "monolith")}
            else:
                edge = (self.state.pos, target) if self.state.pos <= target else (target, self.state.pos)
                if edge not in self.state.blown and self.board.walls.is_blocked(self.state.pos, target):
                    what = t("look.wall", self.lang)
                    status = "blocked"
                    details = {"dir": d.name.lower(), "kind": "wall"}
                else:
                    # Passable: report terrain/object on that cell.
                    # Prefer terrain-like features first.
                    feats = self.board.features.get(target, [])
                    names = {getattr(f, "name", type(f).__name__) for f in feats}
                    terrain = None
                    for f in feats:
                        nm = getattr(f, "name", None)
                        if nm in ("river", "pit"):
                            terrain = f
                            break
                    if terrain is not None and getattr(terrain, "name", None) == "river":
                        kind = getattr(terrain, "kind", "body")
                        what = t(f"look.river.{kind}", self.lang)
                        kind_out = f"river.{kind}"
                    elif terrain is not None and getattr(terrain, "name", None) == "pit":
                        what = t("look.pit", self.lang)
                        kind_out = "pit"
                    elif "treasure" in names:
                        what = t("look.treasure", self.lang)
                        kind_out = "treasure"
                    elif "exit_key" in names:
                        what = t("look.exit_key", self.lang)
                        kind_out = "exit_key"
                    elif "key" in names:
                        what = t("look.key", self.lang)
                        kind_out = "key"
                    elif "boat" in names:
                        what = t("look.boat", self.lang)
                        kind_out = "boat"
                    elif "grenade_pack" in names:
                        what = t("look.grenade_pack", self.lang)
                        kind_out = "grenade_pack"
                    else:
                        what = t("look.empty", self.lang)
                        kind_out = "empty"

                    status = "success"
                    details = {"dir": d.name.lower(), "kind": kind_out}

            # Spend one charge.
            self.state = self.state.copy(flashlight=int(getattr(self.state, "flashlight", 0)) - 1)

            obs = Observation(
                status=status,  # type: ignore[arg-type]
                observe=_observe_cell_from_features(self._features, self.state.pos),
                pos=self.state.pos,
                inventory=self._inventory(),
                forced_chain=(),
                events=("look",),
                details={"look": details},
            )
            txt = t("look.see", self.lang, dir=DIR_WORD[self.lang][action], what=what)
            return StepOutput(obs=obs, text=txt, state=self.state)

        is_bomb = action.startswith("bomb_")
        if is_bomb and not self.grenades_enabled:
            obs = Observation(
                status="blocked",
                observe=_observe_cell_from_features(self._features, self.state.pos),
                pos=self.state.pos,
                inventory=self._inventory(),
                forced_chain=(),
                events=("grenades_disabled",),
            )
            txt = {"ru": "Гранаты отключены.", "en": "Grenades are disabled."}[self.lang]
            return StepOutput(obs=obs, text=txt, state=self.state)
        d = _dir_from_action(action)

        if is_bomb:
            # Blast a wall in the chosen direction (if present) and spend one grenade.
            if self.state.grenades <= 0:
                # Treat as blocked-like narration for now.
                obs = Observation(
                    status="blocked",
                    observe=_observe_cell_from_features(self._features, self.state.pos),
                    pos=self.state.pos,
                    inventory=self._inventory(),
                    forced_chain=(),
                    events=("no_grenades",),
                )
                txt = t("bomb.no_grenades", self.lang)
                return StepOutput(obs=obs, text=txt, state=self.state)

            target = (self.state.pos[0] + d.value[0], self.state.pos[1] + d.value[1])

            # Can't blast outside the board (monolith).
            if not self.board.in_bounds(target):
                obs = Observation(
                    status="blocked",
                    observe=_observe_cell_from_features(self._features, self.state.pos),
                    pos=self.state.pos,
                    inventory=self._inventory(),
                    forced_chain=(),
                    events=("bomb_failed", "monolith"),
                )
                txt = t("bomb.monolith", self.lang)
                return StepOutput(obs=obs, text=txt, state=self.state)

            # Only useful if there is a wall.
            if not self.board.walls.is_blocked(self.state.pos, target):
                obs = Observation(
                    status="success",
                    observe=_observe_cell_from_features(self._features, self.state.pos),
                    pos=self.state.pos,
                    inventory=self._inventory(),
                    forced_chain=(),
                    events=("bomb_no_wall",),
                )
                txt = t("bomb.no_wall", self.lang)
                return StepOutput(obs=obs, text=txt, state=self.state)

            edge = (self.state.pos, target) if self.state.pos <= target else (target, self.state.pos)
            blown = set(self.state.blown)
            blown.add(edge)
            self.state = self.state.copy(
                grenades=self.state.grenades - 1,
                blown=frozenset(blown),
            )

            obs = Observation(
                status="success",
                observe=_observe_cell_from_features(self._features, self.state.pos),
                pos=self.state.pos,
                inventory=self._inventory(),
                forced_chain=(),
                events=("bombed_wall",),
            )
            txt = t("bomb.success", self.lang)
            return StepOutput(obs=obs, text=txt, state=self.state)

        res, tr = attempt_move(self.board, self.state, d)

        verb = DIR_WORD[self.lang][action.replace("bomb_", "")]

        if res is StepResult.BLOCKED:
            reason = tr.log[0] if tr.log else "blocked"
            if reason.startswith("wall"):
                why = t("blocked.wall", self.lang)
            elif reason.startswith("monolith"):
                why = t("blocked.monolith", self.lang)
            else:
                why = t("blocked.generic", self.lang)

            # River rule: even if you fail to move, if you are on a river cell,
            # the current still carries you in the same turn.
            terrain_names = {getattr(f, "name", "") for f in self.board.features.get(self.state.pos, [])}
            if "river" in terrain_names:
                tr2 = resolve_forced(self.board, self.state)
                if tr2.state.pos != self.state.pos:
                    self.state = tr2.state
                    steps = None
                    for fe in getattr(tr2, "forced", ()):
                        if fe.name == "river":
                            steps = fe.steps
                    # If river carried us into the mouth, mention it explicitly.
                    final_river_kind = None
                    for f in self.board.features.get(self.state.pos, []):
                        if getattr(f, "name", None) == "river":
                            final_river_kind = getattr(f, "kind", "body")
                            break

                    if steps in (1, 2):
                        if final_river_kind == "mouth":
                            tail = t(
                                "river.carries.tail.to_mouth",
                                self.lang,
                                cells=cells_phrase(steps, self.lang),
                            )
                        else:
                            tail = t("river.carries.tail", self.lang, cells=cells_phrase(steps, self.lang))
                    else:
                        if final_river_kind == "mouth":
                            tail = t("river.carries.far.to_mouth", self.lang)
                        else:
                            tail = t("river.carries.far", self.lang)

                    obs = Observation(
                        status="success",
                        observe=_observe_cell_from_features(self._features, self.state.pos),
                        pos=self.state.pos,
                        inventory=self._inventory(),
                        forced_chain=(),
                        events=("blocked", "river_carried"),
                        details={
                            "landed": {"kind": "blocked"},
                            "river": {
                                "carried": True,
                                "steps": steps,
                                "to_mouth": final_river_kind == "mouth",
                            },
                        },
                    )
                    txt = t("move.attempt_blocked", self.lang, dir=verb, why=why) + tail
                    return StepOutput(obs=obs, text=txt, state=self.state)

            obs = Observation(
                status="blocked",
                observe=_observe_cell_from_features(self._features, self.state.pos),
                pos=self.state.pos,
                inventory=self._inventory(),
                forced_chain=(),
                events=("blocked",),
                details={"landed": {"kind": "blocked"}},
            )
            txt = t("move.attempt_blocked", self.lang, dir=verb, why=why)
            return StepOutput(obs=obs, text=txt, state=self.state)

        # res == OK
        events = []
        text_parts = []

        # handle "exit edge" attempt: attempt_move keeps position the same and logs it
        if tr.log and tr.log[0] == "exit edge":
            # Base rule: you can only leave if you have the treasure.
            if not self.state.has_treasure:
                obs = Observation(
                    status="success",
                    observe="exit",
                    pos=self.state.pos,
                    inventory=self._inventory(),
                    forced_chain=(),
                    events=("exit_blocked",),
                    details={"landed": {"kind": "exit"}},
                )
                txt = t("exit.found_but_stay", self.lang, dir=verb)
                return StepOutput(obs=obs, text=txt, state=self.state)

            # Optional rule: also require exit key.
            if self.require_exit_key_to_leave and not self.state.has_exit_key:
                obs = Observation(
                    status="success",
                    observe="exit",
                    pos=self.state.pos,
                    inventory=self._inventory(),
                    forced_chain=(),
                    events=("exit_blocked", "exit_key_missing"),
                    details={"landed": {"kind": "exit"}},
                )
                txt = t("exit.found_but_stay.need_exit_key", self.lang, dir=verb)
                return StepOutput(obs=obs, text=txt, state=self.state)

            obs = Observation(
                status="success",
                observe="exit",
                pos=self.state.pos,
                inventory=self._inventory(),
                forced_chain=(),
                events=("exited",),
                details={"landed": {"kind": "exit"}},
            )
            txt = t("exit.leave_with_treasure", self.lang)
            return StepOutput(obs=obs, text=txt, state=self.state)

        # We need to narrate: where the player stepped FIRST (pre-forced),
        # then what mechanisms triggered, then where they ended up.

        landed_pos = getattr(tr, "landed_pos", tr.state.pos)

        # Describe the landed cell (terrain first)
        landed_river_kind = None
        for f in self.board.features.get(landed_pos, []):
            if getattr(f, "name", None) == "river":
                landed_river_kind = getattr(f, "kind", "body")
                break

        landed_observe = _observe_cell_from_features(self._features, landed_pos)

        pit_forced = any(getattr(fe, "name", "") == "pit" for fe in getattr(tr, "forced", ()))

        if pit_forced:
            # Do not claim the cell is "empty"; from the player's POV the key event is the pit.
            text_parts.append(t("move.pit_fell", self.lang, dir=verb))
        else:
            if landed_river_kind is not None:
                landed_where = {
                    "source": t("where.river.source", self.lang),
                    "body": t("where.river.body", self.lang),
                    "mouth": t("where.river.mouth", self.lang),
                }.get(landed_river_kind, t("where.river.body", self.lang))
            else:
                landed_where = {
                    "empty": t("where.empty", self.lang),
                    "key": t("where.key", self.lang),
                    "treasure": t("where.treasure", self.lang),
                    "exit_key": t("where.exit_key", self.lang),
                    "boat": t("where.boat", self.lang),
                    "grenade_pack": t("where.grenade_pack", self.lang),
                }[landed_observe]

            if landed_observe == "boat":
                text_parts.append(t("move.landed.found", self.lang, dir=verb, what=t("where.boat", self.lang)))
            else:
                text_parts.append(t("move.landed", self.lang, dir=verb, where=landed_where))

        # Set current state to landed state (will be further modified by auto-pickups and forced effects)
        self.state = tr.state

        observe = _observe_cell_from_features(self._features, self.state.pos)

        # auto-pickup / auto-open narration + consume items from the world
        # Key pickup
        if observe == "key":
            if prev.has_key:
                if self.reveal_consumed_locations:
                    text_parts.append(t("revisit.key", self.lang))
                events.append("key_already")
            else:
                # pick up key
                self.state = self.state.copy(has_key=True)
                events.append("picked_key")
                text_parts.append(t("pickup.key", self.lang))
                self._consumed.add((self.state.pos, "key"))
                self._features[self.state.pos] = [f for f in self._features.get(self.state.pos, []) if getattr(f, "name", "") != "key"]
                observe = _observe_cell_from_features(self._features, self.state.pos)

        # Boat pickup
        if observe == "boat":
            if not self.state.has_boat:
                self.state = self.state.copy(has_boat=True)
                events.append("picked_boat")
                text_parts.append(t("pickup.boat", self.lang))

            self._consumed.add((self.state.pos, "boat"))
            self._features[self.state.pos] = [f for f in self._features.get(self.state.pos, []) if getattr(f, "name", "") != "boat"]
            observe = _observe_cell_from_features(self._features, self.state.pos)

        # Grenade pack pickup
        if observe == "grenade_pack":
            # Determine how many grenades are on this cell (sum packs, if any)
            cnt = 0
            for f in list(self._features.get(self.state.pos, [])):
                if getattr(f, "name", "") == "grenade_pack":
                    try:
                        cnt += int(getattr(f, "count", 0))
                    except Exception:
                        cnt += 0

            if cnt > 0:
                # Note: grenade packs are also modeled as a Feature in the core transition
                # system, so `attempt_move/resolve_forced` may have already updated
                # `self.state.grenades` on landing. Therefore we must NOT add again here.
                gained = 0
                if self.grenades_enabled:
                    try:
                        gained = max(0, int(self.state.grenades) - int(prev.grenades))
                    except Exception:
                        gained = 0

                events.append("picked_grenades")
                text_parts.append(t("pickup.grenades", self.lang, n=(gained or cnt)))

            self._consumed.add((self.state.pos, "grenade_pack"))
            self._features[self.state.pos] = [f for f in self._features.get(self.state.pos, []) if getattr(f, "name", "") != "grenade_pack"]
            observe = _observe_cell_from_features(self._features, self.state.pos)

        # Exit-key pickup
        if observe == "exit_key":
            if prev.has_exit_key:
                if self.reveal_consumed_locations:
                    text_parts.append(t("revisit.exit_key", self.lang))
                events.append("exit_key_already")
            else:
                self.state = self.state.copy(has_exit_key=True)
                events.append("picked_exit_key")
                text_parts.append(t("pickup.exit_key", self.lang))
                self._consumed.add((self.state.pos, "exit_key"))
                self._features[self.state.pos] = [f for f in self._features.get(self.state.pos, []) if getattr(f, "name", "") != "exit_key"]
                observe = _observe_cell_from_features(self._features, self.state.pos)

        # Treasure chest
        if observe == "treasure":
            if prev.has_treasure:
                if self.reveal_consumed_locations:
                    text_parts.append(t("revisit.treasure", self.lang))
                events.append("treasure_already")
            else:
                if self.state.has_key:
                    self.state = self.state.copy(has_treasure=True)
                    events.append("opened_treasure")
                    text_parts.append(t("treasure.open", self.lang))
                    self._consumed.add((self.state.pos, "treasure"))
                    self._features[self.state.pos] = [f for f in self._features.get(self.state.pos, []) if getattr(f, "name", "") != "treasure"]
                    observe = _observe_cell_from_features(self._features, self.state.pos)
                else:
                    events.append("treasure_locked")
                    text_parts.append(t("treasure.locked", self.lang))

        # Forced effects narration (river, pits, etc.)
        river_steps = None
        for fe in getattr(tr, "forced", ()):
            if fe.name == "pit":
                events.append("pit_fell")
            if fe.name == "river":
                events.append("river_carried")
                if fe.steps in (1, 2):
                    river_steps = fe.steps
                else:
                    river_steps = None

        # Final position description (optional)
        final_pos = self.state.pos
        final_river_kind = None
        for f in self.board.features.get(final_pos, []):
            if getattr(f, "name", None) == "river":
                final_river_kind = getattr(f, "kind", "body")
                break

        # If carried by river:
        # - always say how many cells (if known)
        # - only mention destination explicitly if it is the mouth
        if "river_carried" in events:
            if river_steps in (1, 2):
                # 'в устье' form: without 'в'
                if final_river_kind == "mouth" and final_pos != landed_pos:
                    text_parts.append(
                        t(
                            "river.carried.cells.to_mouth",
                            self.lang,
                            cells=cells_phrase(river_steps, self.lang),
                        )
                    )
                else:
                    text_parts.append(
                        t(
                            "river.carried.cells",
                            self.lang,
                            cells=cells_phrase(river_steps, self.lang),
                        )
                    )
            else:
                if final_river_kind == "mouth" and final_pos != landed_pos:
                    text_parts.append(t("river.carried.to_mouth", self.lang))
                else:
                    text_parts.append(t("river.carried", self.lang))

        if "pit_fell" in events:
            landed_kind = "pit"
        else:
            landed_kind = (
                f"river_{landed_river_kind}" if landed_river_kind is not None else str(landed_observe)
            )

        details: Dict[str, object] = {
            "landed": {"kind": landed_kind},
        }
        # River details only when the player stepped onto a river tile.
        if landed_river_kind is not None:
            details["river"] = {
                "carried": "river_carried" in events,
                "steps": river_steps,
                "to_mouth": (final_river_kind == "mouth") if ("river_carried" in events) else False,
            }

        obs = Observation(
            status="success",
            observe=_observe_cell_from_features(self._features, self.state.pos),  # type: ignore[arg-type]
            pos=self.state.pos,
            inventory=self._inventory(),
            forced_chain=_forced_chain_from_transition(tr, start_pos=self.state.pos),
            events=tuple(events),
            details=details,
        )

        return StepOutput(obs=obs, text=" ".join(text_parts), state=self.state)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "pos": self.state.pos,
            "inventory": self._inventory(),
            "exit_edge": ((self.board.exit_edge[0][0], self.board.exit_edge[0][1]), self.board.exit_edge[1].name.lower()),
        }
