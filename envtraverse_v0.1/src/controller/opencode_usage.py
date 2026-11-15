#!/usr/bin/env python3
"""Collect recorded OpenCode usage without exporting account data or credentials."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

if __package__ in (None, ""):
    # Support the recovery command by absolute path from any experiment root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from opencode_chatgpt_accounts import account_tools


PAGE_SIZE = 100
COLLECTION_TIMEOUT_SECONDS = 600


def native_tokens(value: object) -> dict:
    """Validate all disjoint token categories; missing counts must not become zero."""
    if not isinstance(value, dict) or not isinstance(value.get("cache"), dict):
        raise ValueError("OpenCode session usage must contain tokens and tokens.cache objects")
    categories = [(value, key) for key in ("input", "output", "reasoning")]
    categories += [(value["cache"], key) for key in ("read", "write")]
    for fields, key in categories:
        if type(fields.get(key)) is not int or fields[key] < 0:
            raise ValueError(f"OpenCode session token count {key} must be a non-negative integer")
    return {"input": value["input"], "output": value["output"], "reasoning": value["reasoning"],
            "cache": {"read": value["cache"]["read"], "write": value["cache"]["write"]}}


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, indent=2, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def collect_account_usage(account_path: Path, output_path: Path, *, image: str,
                          game_alias: str, game: str) -> dict:
    """Snapshot every session once. Caller holds the account lock and stops writers.

    Each page is read from the original mounted account with networking disabled.
    Session counters include children, recovery roots, auxiliary requests and any
    failed-step usage that OpenCode persisted. They cannot recover unknown usage.
    """
    for name, value in (("game_alias", game_alias), ("game", game)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a nonempty string for an OpenCode usage snapshot")
    account_tools.require_database(account_path)
    docker = account_tools.Docker(image, COLLECTION_TIMEOUT_SECONDS)
    sessions: dict[str, dict] = {}
    cursor: str | None = None
    cursors: set[str] = set()
    while True:
        command = ["opencode", "api", "session.list", "--standalone",
                   "--param", f"limit={PAGE_SIZE}", "--param", "order=asc"]
        if cursor is not None:
            command += ["--param", "cursor=" + cursor]
        # The controller defers shutdown while collecting. Keep terminal signals
        # from independently terminating its Docker client before the container.
        output = docker.run(account_path, command, operation="OpenCode usage collection", offline=True, isolate=True)
        try:
            page = json.loads(output)
        except (ValueError, UnicodeError):
            raise ValueError("OpenCode usage collection returned invalid JSON") from None
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise ValueError("OpenCode usage collection returned an invalid session page")
        for session in page["data"]:
            if not isinstance(session, dict):
                raise ValueError("OpenCode usage collection returned an invalid session")
            session_id = session.get("id")
            if not isinstance(session_id, str) or not session_id or any(c.isspace() for c in session_id):
                raise ValueError("OpenCode usage collection returned an invalid session ID")
            parent_id = session.get("parentID")
            if parent_id is not None and (not isinstance(parent_id, str) or not parent_id
                                           or any(c.isspace() for c in parent_id)):
                raise ValueError("OpenCode usage collection returned an invalid parent session ID")
            # Never persist titles, metadata, permissions, conversation content,
            # credentials, or native provider responses from the session API.
            record = {"id": session_id, "tokens": native_tokens(session.get("tokens"))}
            if parent_id is not None:
                record["parent_id"] = parent_id
            if session_id in sessions and sessions[session_id] != record:
                raise ValueError("OpenCode session usage changed during collection; stop all account writers")
            sessions[session_id] = record
        pagination = page.get("cursor")
        if not isinstance(pagination, dict) or "next" not in pagination:
            raise ValueError("OpenCode usage collection returned invalid pagination")
        cursor = pagination["next"]
        if cursor is None:
            break
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise ValueError("OpenCode usage collection returned an invalid or repeated cursor")
        cursors.add(cursor)

    tokens = {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}
    for session in sessions.values():
        usage = session["tokens"]
        for key in ("input", "output", "reasoning"):
            tokens[key] += usage[key]
        for key in ("read", "write"):
            tokens["cache"][key] += usage["cache"][key]
    snapshot = {
        "schema_version": 1,
        "source": "opencode.session_counters",
        "status": "collected",
        "scope": "all_recorded_sessions_since_account_cleanup",
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "game_alias": game_alias,
        "game": game,
        "image": image,
        "tokens": tokens,
        "totals": {
            "input_tokens": tokens["input"] + tokens["cache"]["read"] + tokens["cache"]["write"],
            "cached_input_tokens": tokens["cache"]["read"],
            "output_tokens": tokens["output"] + tokens["reasoning"],
        },
        "sessions": sorted(sessions.values(), key=lambda session: session["id"]),
    }
    atomic_write_json(output_path, snapshot)
    return snapshot


def recover_pending_usage(account: Path, *, image: str | None = None) -> Path:
    """Collect a stopped game's saved account before any new online use or cleanup."""
    with account_tools.account_lock(account):
        marker = account_tools.pending_usage_path(account)
        if marker.is_symlink():
            raise ValueError(f"Pending usage marker must not be a symlink: {marker}")
        try:
            pending = json.loads(marker.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError):
            raise ValueError(f"Invalid pending usage marker; account retained: {marker}") from None
        if (not isinstance(pending, dict) or type(pending.get("schema_version")) is not int
                or pending["schema_version"] != 1 or pending.get("account") != str(account)):
            raise ValueError(f"Invalid pending usage marker; account retained: {marker}")
        for key in ("game_alias", "game", "output_path", "image", "container_name"):
            if not isinstance(pending.get(key), str) or not pending[key]:
                raise ValueError(f"Missing {key} in pending usage marker: {marker}")
        output_path = Path(pending["output_path"])
        if not output_path.is_absolute() or output_path.name != "opencode_usage.json":
            raise ValueError(f"Invalid output_path in pending usage marker: {marker}")
        selected_image = image if image is not None else pending["image"]
        if not selected_image or any(c.isspace() for c in selected_image):
            raise ValueError("The collection image must be nonempty and contain no whitespace")
        # A killed controller can leave its game container alive even though the
        # advisory account lock was released. Refuse to race that online writer.
        result = subprocess.run(
            ["docker", "ps", "--all", "--format", "{{.Names}}\t{{.State}}"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        for line in result.stdout.splitlines():
            name, separator, state = line.partition("\t")
            if not separator:
                raise RuntimeError("Could not verify container states before usage recovery")
            # A created container may still be starting under an orphaned Docker
            # client. Paused/restarting containers can likewise resume writing.
            if name == pending["container_name"] and state != "exited":
                raise RuntimeError(f"Stop container {name} (state={state}) before recovering its usage")
        collect_account_usage(account, output_path, image=selected_image,
                              game_alias=pending["game_alias"], game=pending["game"])
        marker.unlink()
        directory = os.open(marker.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recover pending per-game OpenCode usage before reusing an account.")
    parser.add_argument("account", help="original account directory with a pending usage marker")
    parser.add_argument("--image", help="existing image with the same OpenCode version, if the recorded image was removed")
    args = parser.parse_args(argv)

    def interrupt(_signum, _frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        account = account_tools.account_path(args.account)
        path = recover_pending_usage(account, image=args.image)
        print(f"Saved recorded OpenCode usage: {path}")
        return 0
    except (OSError, ValueError, RuntimeError, account_tools.AccountError, subprocess.SubprocessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; pending usage and account data were retained.", file=sys.stderr)
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
