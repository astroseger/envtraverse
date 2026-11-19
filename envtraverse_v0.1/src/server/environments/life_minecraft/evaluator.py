"""Pure, sticky task evaluation; game state never comes from the playing agent."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
import math
import time
from typing import Any, Callable, Iterable

from .catalog import validate_rule


def _inventory(stacks: list[dict[str, Any]], *, equipped: bool = False) -> Counter:
    counts = Counter()
    for stack in stacks:
        if not equipped or stack.get("section") == "armor":
            counts[stack["item"]] += stack["count"]
    return counts


class TaskEvaluator:
    """Call tick even without actions. A verdict never changes after it is set.

    The runtime keeps Minecraft and capture running after a timeout. It reports
    this latched verdict as terminal only when the next action arrives.
    """

    def __init__(self, task: dict[str, Any], baseline: dict[str, Any], minecraft_task_timeout_seconds: float,
                 *, clock: Callable[[], float] = time.monotonic):
        validate_rule(task["success"])
        if type(minecraft_task_timeout_seconds) not in (float, int) or not math.isfinite(minecraft_task_timeout_seconds) or minecraft_task_timeout_seconds <= 0:
            raise ValueError("minecraft_task_timeout_seconds must be positive and finite")
        if not baseline.get("connected") or not baseline.get("session_id"):
            raise ValueError("Evaluation requires a connected player and server session")
        self.task, self.baseline = deepcopy(task), deepcopy(baseline)
        self.clock, self.started = clock, clock()
        self.minecraft_task_timeout_seconds = minecraft_task_timeout_seconds
        self.deadline = self.started + minecraft_task_timeout_seconds
        self.result: dict[str, Any] | None = None
        self.last_event_seq = baseline.get("last_event_seq", 0)
        self._entries: set[str] = set()
        self._transitions: set[str] = set()
        self._event_crafts: Counter = Counter()
        self._event_kills: Counter = Counter()
        self._event_dragon_kills = 0

    @property
    def terminal(self) -> bool:
        return self.result is not None

    def _finish(self, status: str, reason: str) -> dict[str, Any]:
        if self.result is None:
            self.result = {"status": status, "reason": reason, "success": status == "succeeded",
                           "task_id": self.task["id"], "elapsed_seconds": max(0, self.clock() - self.started)}
        return self.result

    def tick(self) -> dict[str, Any] | None:
        if self.result is None and self.clock() >= self.deadline:
            self._finish("failed", "minecraft_task_timeout")
        return self.result

    def invalidate(self, reason: str) -> dict[str, Any]:
        self.tick()
        return self._finish("invalid", reason)

    def cancel(self) -> dict[str, Any]:
        self.tick()
        return self._finish("cancelled", "cancelled")

    def _transition_rules(self, rule: dict[str, Any]):
        if rule["type"] == "transition":
            yield rule
        for child in rule.get("rules", []):
            yield from self._transition_rules(child)

    def _events(self, events: Iterable[dict[str, Any]]) -> None:
        for event in events:
            if event.get("session_id") != self.baseline["session_id"]:
                self.invalidate("server_session_changed")
                return
            seq = event.get("seq", 0)
            if seq <= self.last_event_seq:
                continue
            self.last_event_seq = seq
            kind, data = event.get("type"), event.get("data", {})
            if kind == "disconnect":
                self.invalidate("player_disconnected")
                return
            if kind == "craft":
                self._event_crafts[data["item"]] += data["count"]
            elif kind == "kill" and data.get("credited_to_player") is True:
                self._event_kills[data["entity_type"]] += 1
            elif kind == "dragon_defeated" and data.get("credited_to_player") is True:
                self._event_dragon_kills += 1
            elif kind == "dimension_change":
                self._entries.add(data["dimension"])
                for rule in self._transition_rules(self.task["success"]):
                    if data.get("from_dimension") == rule["from_dimension"] and data["dimension"] == rule["dimension"]:
                        counts = _inventory(data.get("inventory", []))
                        if all(counts[item] >= count for item, count in rule["inventory"].items()):
                            self._transitions.add(json.dumps(rule, sort_keys=True))

    def _matches(self, rule: dict[str, Any], state: dict[str, Any]) -> bool:
        kind = rule["type"]
        counters, base = state.get("counters", {}), self.baseline.get("counters", {})
        stacks = (state.get("player") or {}).get("inventory", [])
        if kind == "all":
            return all(self._matches(child, state) for child in rule["rules"])
        if kind == "any":
            return any(self._matches(child, state) for child in rule["rules"])
        if kind in ("inventory", "equipped"):
            return _inventory(stacks, equipped=kind == "equipped")[rule["item"]] >= rule["count"]
        if kind == "inventory_any":
            counts = _inventory(stacks)
            return sum(counts[item] for item in rule["items"]) >= rule["count"]
        if kind in ("crafted", "kill"):
            name, item = ("crafted", rule["item"]) if kind == "crafted" else ("kills", rule["entity"])
            count = counters.get(name, {}).get(item, 0) - base.get(name, {}).get(item, 0)
            event_count = (self._event_crafts if kind == "crafted" else self._event_kills)[item]
            return max(count, event_count) >= rule["count"]
        if kind == "dimension_entry":
            return rule["dimension"] in self._entries
        if kind == "transition":
            return json.dumps(rule, sort_keys=True) in self._transitions
        if kind == "dragon_kill":
            return max(counters.get("dragon_kills", 0) - base.get("dragon_kills", 0), self._event_dragon_kills) >= rule["count"]
        raise ValueError("Unsupported rule")

    def observe(self, state: dict[str, Any], events: Iterable[dict[str, Any]] = (), *, fresh: bool = True) -> dict[str, Any] | None:
        if self.tick() is not None:
            return self.result
        if not fresh or state.get("telemetry_error") or not state.get("server_running", True):
            return self.invalidate("telemetry_unavailable")
        if state.get("session_id") != self.baseline["session_id"]:
            return self.invalidate("server_session_changed")
        if not state.get("connected"):
            return self.invalidate("player_disconnected")
        if state.get("counters", {}).get("disconnects", 0) > self.baseline.get("counters", {}).get("disconnects", 0):
            return self.invalidate("player_disconnected")
        self._events(events)
        if self.terminal:
            return self.result
        if state.get("hardcore") and state.get("counters", {}).get("deaths", 0) > self.baseline.get("counters", {}).get("deaths", 0):
            return self._finish("failed", "hardcore_death")
        if self._matches(self.task["success"], state):
            return self._finish("succeeded", "goal_achieved")
        return None
