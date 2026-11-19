"""Versioned task catalog with declarative, validated success rules."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Any

_IDENTIFIER = re.compile(r"minecraft:[a-z0-9_/.]+\Z")
CATALOG_SCHEMA_VERSION = 1


def validate_rule(rule: Any, *, depth: int = 0) -> None:
    if depth > 12 or not isinstance(rule, dict):
        raise ValueError("Success rules must be bounded objects")
    kind = rule.get("type")
    fields = {
        "all": {"type", "rules"}, "any": {"type", "rules"},
        "crafted": {"type", "item", "count"},
        "inventory": {"type", "item", "count"},
        "inventory_any": {"type", "items", "count"},
        "equipped": {"type", "item", "count"},
        "kill": {"type", "entity", "count"},
        "kill_any": {"type", "entities", "count"},
        "dimension_entry": {"type", "dimension"},
        "transition": {"type", "from_dimension", "dimension", "inventory"},
        "dragon_kill": {"type", "count"},
    }
    if not isinstance(kind, str) or kind not in fields or set(rule) != fields[kind]:
        raise ValueError("Unknown rule type or incorrect rule fields")
    if kind in ("all", "any"):
        if not isinstance(rule["rules"], list) or not 1 <= len(rule["rules"]) <= 64:
            raise ValueError("Composite rules require 1..64 children")
        for child in rule["rules"]:
            validate_rule(child, depth=depth + 1)
    if "count" in rule and (type(rule["count"]) is not int or not 1 <= rule["count"] <= 1000000):
        raise ValueError("Rule count must be a positive integer")
    for field in ("item", "entity", "dimension", "from_dimension"):
        if field in rule and (not isinstance(rule[field], str) or not _IDENTIFIER.fullmatch(rule[field])):
            raise ValueError(f"{field} must be a Minecraft identifier")
    if kind in ("inventory_any", "kill_any"):
        field = "items" if kind == "inventory_any" else "entities"
        identifiers = rule[field]
        if (not isinstance(identifiers, list) or not identifiers
                or any(not isinstance(value, str) or not _IDENTIFIER.fullmatch(value) for value in identifiers)
                or len(set(identifiers)) != len(identifiers)):
            raise ValueError(f"{field} must be a nonempty list of unique Minecraft identifiers")
    if kind == "transition":
        if not isinstance(rule["inventory"], dict) or not rule["inventory"]:
            raise ValueError("Transition inventory must be a nonempty item/count mapping")
        for item, count in rule["inventory"].items():
            validate_rule({"type": "inventory", "item": item, "count": count}, depth=depth + 1)


def list_tasks() -> list[dict[str, Any]]:
    with Path(__file__).with_name("tasks.json").open(encoding="utf-8") as source:
        catalog = json.load(source)
    if catalog.get("schema_version") != CATALOG_SCHEMA_VERSION or not isinstance(catalog.get("tasks"), list):
        raise ValueError("Unsupported task catalog schema")
    seen = set()
    for task in catalog["tasks"]:
        required = {"id", "instruction", "requires_hostiles", "success"}
        if not isinstance(task, dict) or not required <= set(task) or set(task) - required - {"rank"}:
            raise ValueError("Invalid task definition")
        if not isinstance(task["id"], str) or not re.fullmatch(r"[a-z][a-z0-9_]*", task["id"]) or task["id"] in seen:
            raise ValueError("Invalid or duplicate task ID")
        seen.add(task["id"])
        if "rank" in task and (type(task["rank"]) is not int or task["rank"] < 1):
            raise ValueError("Invalid optional task rank")
        if type(task["requires_hostiles"]) is not bool:
            raise ValueError("Invalid task hostility requirement")
        if not isinstance(task["instruction"], str) or not task["instruction"].strip():
            raise ValueError("Task instruction must not be empty")
        validate_rule(task["success"])
    return deepcopy(catalog["tasks"])


def get_task(game_id: str) -> dict[str, Any]:
    for task in list_tasks():
        if task["id"] == game_id:
            return task
    raise ValueError("Unknown Minecraft task ID")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="List the predefined life_minecraft tasks")
    parser.add_argument("--json", action="store_true", help="print the complete formal catalog as JSON")
    arguments = parser.parse_args()
    tasks = list_tasks()
    if arguments.json:
        print(json.dumps({"schema_version": CATALOG_SCHEMA_VERSION, "tasks": tasks}, indent=2))
    else:
        for task in tasks:
            print(f'{task["id"]}: {task["instruction"]}')
