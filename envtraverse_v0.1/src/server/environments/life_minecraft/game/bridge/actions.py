"""Validate the complete action before any input side effects."""
from __future__ import annotations

from typing import Any

KEYS = frozenset([*"wasdeqf123456789", "space", "shift", "ctrl", "tab", "escape"])
BUTTONS = frozenset(("left", "middle", "right"))


def _bad(message: str) -> None:
    raise ValueError(message)


def validate_action(action: Any, *, width: int = 960, height: int = 540) -> dict[str, Any]:
    if not isinstance(action, dict) or not isinstance(action.get("id"), str):
        _bad("Action must be an object with a string id")
    specs = {
        "key_down": ({"id", "key"}, set()),
        "key_up": ({"id", "key"}, set()),
        "key_press": ({"id", "key"}, {"duration_ms"}),
        "mouse_move": ({"id"}, {"x", "y", "dx", "dy"}),
        "mouse_down": ({"id", "button"}, set()),
        "mouse_up": ({"id", "button"}, set()),
        "mouse_click": ({"id", "button"}, {"x", "y"}),
        "mouse_scroll": ({"id", "amount"}, set()),
        "release_all": ({"id"}, set()),
    }
    kind = action["id"]
    if kind not in specs:
        _bad("Unknown action id")
    required, optional = specs[kind]
    if not required <= action.keys() or action.keys() - required - optional:
        _bad(f"Incorrect fields for {kind}")
    result = dict(action)
    if "key" in required and (not isinstance(action["key"], str) or action["key"] not in KEYS):
        _bad("Unsupported key")
    if "button" in required and (not isinstance(action["button"], str) or action["button"] not in BUTTONS):
        _bad("Unsupported mouse button")
    if kind == "key_press":
        value = action.get("duration_ms", 250)
        if type(value) is not int or not 1 <= value <= 10000:
            _bad("duration_ms must be an integer in [1, 10000]")
        result["duration_ms"] = value
    if kind in ("mouse_move", "mouse_click"):
        absolute = "x" in action or "y" in action
        relative = "dx" in action or "dy" in action
        if absolute:
            if relative or not {"x", "y"} <= action.keys():
                _bad("Supply x and y together, without dx or dy")
            for name, bound in (("x", width), ("y", height)):
                if type(action[name]) is not int or not 0 <= action[name] < bound:
                    _bad(f"{name} must be an integer in [0, {bound})")
        elif kind == "mouse_move":
            if not relative:
                _bad("mouse_move requires x/y or dx/dy")
            for name in ("dx", "dy"):
                value = action.get(name, 0)
                if type(value) is not int or not -4096 <= value <= 4096:
                    _bad(f"{name} must be an integer in [-4096, 4096]")
                result[name] = value
    if kind == "mouse_scroll":
        if type(action["amount"]) is not int or not -10 <= action["amount"] <= 10:
            _bad("amount must be an integer in [-10, 10]")
    return result
