from copy import deepcopy

import pytest

from ..catalog import get_task, list_tasks, validate_rule
from ..evaluator import TaskEvaluator


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


def state():
    return {"session_id": "server-session", "connected": True, "server_running": True,
            "last_event_seq": 10, "hardcore": False,
            "player": {"inventory": [], "dimension": "minecraft:overworld"},
            "counters": {"crafted": {}, "kills": {}, "deaths": 0, "disconnects": 0,
                         "dragon_kills": 0, "visited_dimensions": ["minecraft:the_nether"]}}


def event(kind, data, seq=11):
    return {"session_id": "server-session", "seq": seq, "type": kind, "data": data}


def stack(item, count=1, section="main"):
    return {"item": "minecraft:" + item, "count": count, "section": section}


def evaluator(task="craft_wooden_pickaxe", baseline=None, timeout=10):
    clock = Clock()
    return TaskEvaluator(get_task(task), baseline or state(), timeout, clock=clock), clock


def test_catalog_has_unique_valid_tasks_without_required_ranks():
    tasks = list_tasks()
    assert len(tasks) == len({task["id"] for task in tasks}) == 25
    assert all("rank" not in task for task in tasks)
    for task in tasks:
        validate_rule(task["success"])


def test_inventory_pickaxe_is_not_proof_of_crafting():
    check, _ = evaluator()
    current = state()
    current["player"]["inventory"] = [stack("wooden_pickaxe")]
    assert check.observe(current) is None
    current["counters"]["crafted"]["minecraft:wooden_pickaxe"] = 1
    assert check.observe(current)["status"] == "succeeded"


def test_crafting_baseline_and_event_snapshot_counts_do_not_double_count():
    initial = state()
    initial["counters"]["crafted"]["minecraft:ender_eye"] = 4
    check, _ = evaluator("craft_eyes_of_ender", initial)
    current = deepcopy(initial)
    current["counters"]["crafted"]["minecraft:ender_eye"] = 10
    craft = event("craft", {"item": "minecraft:ender_eye", "count": 6})
    assert check.observe(current, [craft]) is None
    assert check.observe(current, [craft]) is None
    current["counters"]["crafted"]["minecraft:ender_eye"] = 16
    assert check.observe(current)["success"] is True


def test_timeout_without_action_irreversible_even_if_goal_achieved_later():
    check, clock = evaluator()
    clock.value = 10
    result = check.tick()
    assert result["status"] == "failed"
    assert result["reason"] == "minecraft_task_timeout"
    current = state()
    current["counters"]["crafted"]["minecraft:wooden_pickaxe"] = 1
    assert check.observe(current) is result
    assert check.cancel() is result


def test_success_before_deadline_is_not_replaced_by_timeout():
    check, clock = evaluator()
    clock.value = 9.9
    result = check.observe(state(), [event("craft", {"item": "minecraft:wooden_pickaxe", "count": 1})])
    clock.value = 100
    assert check.tick() is result
    assert result["success"]


def test_at_deadline_timeout_wins_over_new_goal_observation():
    check, clock = evaluator()
    clock.value = 10
    result = check.observe(state(), [event("craft", {"item": "minecraft:wooden_pickaxe", "count": 1})])
    assert result["reason"] == "minecraft_task_timeout"


def test_armor_must_be_equipped_simultaneously_not_once_each():
    check, _ = evaluator("craft_and_equip_iron_armor")
    current = state()
    pieces = ["iron_helmet", "iron_chestplate", "iron_leggings", "iron_boots"]
    current["counters"]["crafted"] = {"minecraft:" + piece: 1 for piece in pieces}
    for piece in pieces:
        current["player"]["inventory"] = [stack(piece, section="armor")]
        assert check.observe(current) is None
    current["player"]["inventory"] = [stack(piece) for piece in pieces]
    assert check.observe(current) is None
    current["player"]["inventory"] = [stack(piece, section="armor") for piece in pieces]
    assert check.observe(current)["success"]


def test_cooked_food_combines_allowlisted_items_only():
    check, _ = evaluator("collect_cooked_food")
    current = state()
    current["player"]["inventory"] = [stack("cooked_beef", 3), stack("baked_potato", 4), stack("beef", 64)]
    assert check.observe(current) is None
    current["player"]["inventory"].append(stack("cooked_cod"))
    assert check.observe(current)["success"]


def test_dimension_requires_entry_event_after_baseline_not_visited_flag():
    check, _ = evaluator("enter_nether")
    current = state()
    current["player"]["dimension"] = "minecraft:the_nether"
    assert check.observe(current, [event("dimension_change", {"dimension": "minecraft:the_nether"}, seq=10)]) is None
    assert check.observe(current, [event("dimension_change", {"dimension": "minecraft:the_nether"})])["success"]


def test_transition_checks_inventory_at_crossing_not_later():
    check, _ = evaluator("return_with_blaze_rods")
    current = state()
    current["player"]["inventory"] = [stack("blaze_rod", 6)]
    crossing = {"from_dimension": "minecraft:the_nether", "dimension": "minecraft:overworld", "inventory": [stack("blaze_rod", 5)]}
    assert check.observe(current, [event("dimension_change", crossing)]) is None
    crossing["inventory"] = [stack("blaze_rod", 6)]
    current["player"]["inventory"] = []
    assert check.observe(current, [event("dimension_change", crossing, seq=12)])["success"]


def test_player_credited_kills_and_baseline_are_required():
    initial = state()
    initial["counters"]["kills"]["minecraft:zombie"] = 2
    check, _ = evaluator("kill_zombie", initial)
    assert check.observe(initial, [event("entity_death", {"entity_type": "minecraft:zombie"})]) is None
    assert check.observe(initial, [event("kill", {"entity_type": "minecraft:zombie", "credited_to_player": False}, seq=12)]) is None
    assert check.observe(initial, [event("kill", {"entity_type": "minecraft:zombie", "credited_to_player": True}, seq=13)])["success"]



@pytest.mark.parametrize("task,entity", [
    ("kill_zombie", "zombie"), ("kill_zombie", "husk"),
    ("kill_spider", "spider"), ("kill_spider", "cave_spider"),
    ("kill_skeleton", "skeleton"), ("kill_enderman", "enderman"),
    ("kill_ghast", "ghast"),
])
@pytest.mark.parametrize("source", ["snapshot", "event"])
def test_mob_goals_accept_player_credited_kills_from_either_source(task, entity, source):
    initial = state()
    entity = "minecraft:" + entity
    initial["counters"]["kills"][entity] = 5
    check, _ = evaluator(task, initial)
    assert check.observe(initial) is None
    current = deepcopy(initial)
    events = []
    if source == "snapshot":
        current["counters"]["kills"][entity] += 1
    else:
        events.append(event("kill", {"entity_type": entity, "credited_to_player": True}))
    assert check.observe(current, events)["success"]
    assert get_task(task)["requires_hostiles"] is True


@pytest.mark.parametrize("spiders,cave_spiders", [(3, 0), (2, 1), (1, 2), (0, 3)])
def test_three_spiders_combines_variants_and_ignores_previous_kills(spiders, cave_spiders):
    initial = state()
    initial["counters"]["kills"] = {"minecraft:spider": 7, "minecraft:cave_spider": 9}
    check, _ = evaluator("kill_three_spiders", initial)
    assert check.observe(initial) is None
    current = deepcopy(initial)
    current["counters"]["kills"]["minecraft:spider"] += spiders
    current["counters"]["kills"]["minecraft:cave_spider"] += cave_spiders
    last = "minecraft:spider" if spiders else "minecraft:cave_spider"
    current["counters"]["kills"][last] -= 1
    assert check.observe(current) is None
    current["counters"]["kills"][last] += 1
    assert check.observe(current)["success"]


@pytest.mark.parametrize("task,unrelated", [
    ("kill_zombie", "drowned"), ("kill_spider", "silverfish"),
    ("kill_three_spiders", "zombie"), ("kill_skeleton", "wither_skeleton"),
    ("kill_enderman", "endermite"), ("kill_ghast", "blaze"),
])
def test_mob_goals_exclude_unlisted_entities(task, unrelated):
    check, _ = evaluator(task)
    current = state()
    entity = "minecraft:" + unrelated
    current["counters"]["kills"][entity] = 100
    assert check.observe(current, [event("kill", {"entity_type": entity, "credited_to_player": True})]) is None


def test_group_kill_replayed_events_and_snapshots_do_not_double_count():
    check, _ = evaluator("kill_three_spiders")
    current = state()
    current["counters"]["kills"] = {"minecraft:spider": 1, "minecraft:cave_spider": 1}
    events = [event("kill", {"entity_type": "minecraft:spider", "credited_to_player": True}, seq=11),
              event("kill", {"entity_type": "minecraft:cave_spider", "credited_to_player": True}, seq=12)]
    assert check.observe(current, events) is None
    assert check.observe(current, events) is None
    third = event("kill", {"entity_type": "minecraft:cave_spider", "credited_to_player": True}, seq=13)
    assert check.observe(current, [third])["success"]


def test_group_kill_combines_entities_when_their_event_and_snapshot_counts_differ():
    initial = state()
    initial["counters"]["kills"] = {"minecraft:spider": 10, "minecraft:cave_spider": 5}
    check, _ = evaluator("kill_three_spiders", initial)
    current = deepcopy(initial)
    current["counters"]["kills"]["minecraft:spider"] += 2
    # The snapshot knows about two spiders; the events know about one of those
    # spiders and one newer cave spider. There are three distinct kills in total.
    events = [event("kill", {"entity_type": "minecraft:spider", "credited_to_player": True}, seq=11),
              event("kill", {"entity_type": "minecraft:cave_spider", "credited_to_player": True}, seq=12)]
    assert check.observe(current, events)["success"]


def test_group_kill_requires_player_credit_and_events_after_baseline():
    check, _ = evaluator("kill_spider")
    current = state()
    events = [event("kill", {"entity_type": "minecraft:spider", "credited_to_player": True}, seq=10),
              event("entity_death", {"entity_type": "minecraft:cave_spider"}, seq=11),
              event("kill", {"entity_type": "minecraft:cave_spider", "credited_to_player": False}, seq=12)]
    assert check.observe(current, events) is None
    assert check.observe(current, [event("kill", {
        "entity_type": "minecraft:cave_spider", "credited_to_player": True}, seq=13)])["success"]


def test_three_skeleton_goal_still_requires_three_kills():
    check, _ = evaluator("kill_three_skeletons")
    current = state()
    current["counters"]["kills"]["minecraft:skeleton"] = 2
    assert check.observe(current) is None
    current["counters"]["kills"]["minecraft:skeleton"] = 3
    assert check.observe(current)["success"]


def test_dragon_requires_completed_defeat_with_player_credit():
    check, _ = evaluator("kill_ender_dragon")
    current = state()
    current["counters"]["kills"]["minecraft:ender_dragon"] = 1
    assert check.observe(current) is None
    assert check.observe(current, [event("dragon_defeated", {"credited_to_player": False})]) is None
    assert check.observe(current, [event("dragon_defeated", {"credited_to_player": True}, seq=12)])["success"]


def test_respawn_allowed_but_hardcore_death_fails():
    check, _ = evaluator()
    current = state()
    current["counters"]["deaths"] = 1
    assert check.observe(current) is None
    current["hardcore"] = True
    assert check.observe(current)["reason"] == "hardcore_death"


@pytest.mark.parametrize("change,reason", [
    ({"connected": False}, "player_disconnected"),
    ({"session_id": "new-session"}, "server_session_changed"),
    ({"telemetry_error": True}, "telemetry_unavailable"),
    ({"server_running": False}, "telemetry_unavailable"),
])
def test_invalid_attempt_remains_invalid_after_recovery(change, reason):
    check, _ = evaluator()
    current = state()
    current.update(change)
    result = check.observe(current)
    assert result["status"] == "invalid" and result["reason"] == reason
    assert check.observe(state()) is result


@pytest.mark.parametrize("rule", [
    {"type": "execute", "code": "anything"},
    {"type": "crafted", "item": "minecraft:diamond", "count": True},
    {"type": "all", "rules": []},
    {"type": "inventory_any", "items": ["minecraft:diamond", "minecraft:diamond"], "count": 1},
    {"type": "kill_any", "entities": [], "count": 1},
    {"type": "kill_any", "entities": ["minecraft:spider", "minecraft:spider"], "count": 3},
    {"type": "kill_any", "entities": ["spider"], "count": 1},
    {"type": "kill_any", "entities": [None], "count": 1},
    {"type": "kill_any", "entities": "minecraft:spider", "count": 1},
    {"type": "kill_any", "entities": ["minecraft:spider"], "count": True},
    {"type": "kill_any", "entities": ["minecraft:spider"], "count": 0},
    {"type": "kill_any", "entities": ["minecraft:spider"], "count": 1, "entity": "minecraft:spider"},
])
def test_invalid_rules_rejected(rule):
    with pytest.raises(ValueError):
        validate_rule(rule)
