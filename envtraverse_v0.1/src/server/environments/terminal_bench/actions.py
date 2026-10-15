"""Validate an entire action before performing any side effects."""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any
from .protocol import RuntimeFault

KEY_ALIASES = {"PageUp": "PPage", "PageDown": "NPage", "Delete": "DC", "Insert": "IC"}
KEY_NAMES = {
    "Enter", "Escape", "Tab", "BTab", "BSpace", "Space", "Up", "Down", "Left",
    "Right", "Home", "End", *KEY_ALIASES,
    *(f"F{i}" for i in range(1, 13)),
    *(f"{modifier}-{letter}" for modifier in ("C", "M") for letter in "abcdefghijklmnopqrstuvwxyz"),
}


def _bad(message: str) -> None:
    raise RuntimeFault("invalid_action", message)


def validate_action(action: Any) -> dict[str, Any]:
    if not isinstance(action, dict) or not isinstance(action.get("id"), str):
        _bad("Action must be an object with a string id")
    kind = action["id"]
    specs = {
        "terminal_new": ({"id"}, set()),
        "terminal_send": ({"id", "terminal_id", "keys"}, set()),
        "terminal_read": ({"id", "terminal_id"}, {"view", "wait_ms", "lines"}),
        "get_file": ({"id", "path"}, set()),
        "send_path": ({"id", "path_local", "path_sandbox"}, set()),
        "finish": ({"id"}, set()),
    }
    if kind not in specs:
        _bad("Unknown action id")
    required, optional = specs[kind]
    if not required <= action.keys() or action.keys() - required - optional:
        _bad(f"Incorrect fields for {kind}")
    if "terminal_id" in required:
        tid = action["terminal_id"]
        if not isinstance(tid, str) or re.fullmatch(r"t[1-9][0-9]*", tid) is None:
            _bad("terminal_id must be an ID returned by this environment")
    if kind == "terminal_send":
        keys = action["keys"]
        if not isinstance(keys, list) or not 1 <= len(keys) <= 256:
            _bad("keys must contain 1..256 text/key objects")
        size = 0
        for item in keys:
            if not isinstance(item, dict) or set(item) not in ({"text"}, {"key"}):
                _bad("Each input item must contain exactly one of text or key")
            value = next(iter(item.values()))
            if not isinstance(value, str) or "\0" in value:
                _bad("Input values must be strings without NUL")
            try:
                size += len(value.encode("utf-8"))
            except UnicodeEncodeError:
                _bad("Input must be valid Unicode")
            if "key" in item and value not in KEY_NAMES:
                _bad("Unsupported named key")
        if size > 65536:
            _bad("A send action is limited to 65536 UTF-8 bytes")
    elif kind == "terminal_read":
        if action.get("view", "screen") not in ("screen", "history"):
            _bad("view must be screen or history")
        for key, default, minimum, maximum in (("wait_ms", 0, 0, 2000), ("lines", 200, 1, 2000)):
            value = action.get(key, default)
            if type(value) is not int or not minimum <= value <= maximum:
                _bad(f"{key} must be an integer between {minimum} and {maximum}")
    elif kind == "get_file":
        path = action["path"]
        if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
            _bad("path must be an absolute sandbox path without NUL")
        try:
            if len(path.encode("utf-8")) > 4096:
                _bad("File path is too long")
        except UnicodeEncodeError:
            _bad("Path must be valid Unicode")
    elif kind == "send_path":
        for name in ("path_local", "path_sandbox"):
            path = action[name]
            if not isinstance(path, str) or not path or "\0" in path:
                _bad(f"{name} must be a nonempty path without NUL")
            try:
                if len(path.encode("utf-8")) > 4096:
                    _bad(f"{name} is too long")
            except UnicodeEncodeError:
                _bad(f"{name} must be valid Unicode")
        local = PurePosixPath(action["path_local"])
        if local.is_absolute() or ".." in local.parts:
            _bad("path_local must be relative to your workspace, without '..'")
        if not action["path_sandbox"].startswith("/"):
            _bad("path_sandbox must be an absolute sandbox path")
    return action
