#!/usr/bin/env python3
"""Extract per-game token counts, including failed setup, without cost estimates."""
from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
from typing import Any

if __package__:
    from .server_logs import game_server_logs, read_session, read_start_attempt
else:
    from server_logs import game_server_logs, read_session, read_start_attempt


TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens")
OPENCODE_USAGE_FILENAME = "opencode_usage.json"


def read_opencode_usage_snapshot(path: Path) -> dict[str, int]:
    """Validate the saved session counters before trusting their common totals."""
    def invalid(message: str) -> ValueError:
        return ValueError(f"{path}: invalid OpenCode usage snapshot: {message}")

    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise invalid(f"malformed JSON ({exc.msg})") from None
    if not isinstance(snapshot, dict):
        raise invalid("expected an object")
    if type(snapshot.get("schema_version")) is not int or snapshot["schema_version"] != 1:
        raise invalid("unsupported schema_version; expected 1")
    if snapshot.get("source") != "opencode.session_counters" or snapshot.get("status") != "collected":
        raise invalid("expected collected opencode.session_counters")
    for field in ("game_alias", "game", "collected_at"):
        if not isinstance(snapshot.get(field), str) or not snapshot[field].strip():
            raise invalid(f"{field} must be a nonempty string")
    try:
        collected_at = datetime.fromisoformat(snapshot["collected_at"].replace("Z", "+00:00"))
    except ValueError:
        raise invalid("collected_at must be a UTC ISO timestamp") from None
    if collected_at.utcoffset() != timedelta(0):
        raise invalid("collected_at must be a UTC ISO timestamp")

    def native_counts(value: object, field: str) -> dict[str, int]:
        if not isinstance(value, dict) or set(value) != {"input", "output", "reasoning", "cache"}:
            raise invalid(f"{field} must contain input, output, reasoning and cache")
        cache = value["cache"]
        if not isinstance(cache, dict) or set(cache) != {"read", "write"}:
            raise invalid(f"{field}.cache must contain read and write")
        counts = {"input": value["input"], "output": value["output"], "reasoning": value["reasoning"],
                  "cache.read": cache["read"], "cache.write": cache["write"]}
        for name, count in counts.items():
            if type(count) is not int or count < 0:
                raise invalid(f"{field}.{name} must be a non-negative integer")
        return counts

    aggregate = native_counts(snapshot.get("tokens"), "tokens")
    sessions = snapshot.get("sessions")
    if not isinstance(sessions, list):
        raise invalid("sessions must be a list")
    seen = set()
    summed = dict.fromkeys(aggregate, 0)
    for index, session in enumerate(sessions):
        field = f"sessions[{index}]"
        if not isinstance(session, dict):
            raise invalid(f"{field} must be an object")
        session_id = session.get("id")
        if not isinstance(session_id, str) or not session_id.strip() or session_id in seen:
            raise invalid(f"{field}.id must be a unique nonempty string")
        seen.add(session_id)
        parent = session.get("parent_id")
        if parent is not None and (not isinstance(parent, str) or not parent.strip()):
            raise invalid(f"{field}.parent_id must be a nonempty string or null")
        for name, count in native_counts(session.get("tokens"), f"{field}.tokens").items():
            summed[name] += count
    if summed != aggregate:
        raise invalid("tokens do not match the sum of session counters")

    totals = snapshot.get("totals")
    if not isinstance(totals, dict) or set(totals) != set(TOKEN_FIELDS):
        raise invalid("totals must contain all three common token fields")
    for field, count in totals.items():
        if type(count) is not int or count < 0:
            raise invalid(f"totals.{field} must be a non-negative integer")
    expected = {"input_tokens": aggregate["input"] + aggregate["cache.read"] + aggregate["cache.write"],
                "cached_input_tokens": aggregate["cache.read"],
                "output_tokens": aggregate["output"] + aggregate["reasoning"]}
    if totals != expected:
        raise invalid("totals do not match the native token counters")
    return {field: totals[field] for field in TOKEN_FIELDS}


def extract_token_counts(
    log_path: Path, *, readable: Callable[[Path], bool] | None = None,
) -> dict[str, int]:
    """Prefer recorded OpenCode session usage, otherwise extract stdout counters.

    Repeated thread.started events (resumes/recovery) do not reset a thread's
    counters. The OpenCode stdout fallback deduplicates completed steps by
    session/message and warns that root-session output is incomplete.
    """
    snapshot = log_path.with_name(OPENCODE_USAGE_FILENAME)
    source = snapshot if snapshot.exists() or snapshot.is_symlink() else log_path
    if readable is not None and not readable(source):
        raise FileNotFoundError(f"Token usage unavailable: {source}")
    if source == snapshot:
        return read_opencode_usage_snapshot(source)
    opencode_detected = any(log_path.with_name(name).exists() for name in
                            (".opencode_session_id", "opencode_events.jsonl", "opencode_stderr.log"))
    thread_usage: dict[str, dict[str, int] | None] = {}
    step_usage: dict[tuple[str, str], dict[str, int]] = {}
    current_thread_id: str | None = None

    with log_path.open(encoding="utf-8") as log:
        for line_number, raw_line in enumerate(log, start=1):
            line = raw_line.strip()
            if not line.startswith("{"):
                continue
            location = f"{log_path}:{line_number}"
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                print(f"Warning: {location}: skipping malformed JSON object", file=sys.stderr)
                continue

            if (isinstance(event.get("sessionID"), str)
                    or event.get("type") in {"step_start", "step_finish", "tool_use", "reasoning", "background_wait"}):
                opencode_detected = True

            if event.get("type") == "thread.started":
                thread_id = event.get("thread_id")
                if not isinstance(thread_id, str) or not thread_id.strip():
                    print(f"Warning: {location}: thread.started without thread_id", file=sys.stderr)
                    current_thread_id = None
                    continue
                current_thread_id = thread_id
                thread_usage.setdefault(thread_id, None)
            elif event.get("type") == "turn.completed":
                if current_thread_id is None:
                    print(f"Warning: {location}: turn.completed without active thread", file=sys.stderr)
                    continue
                usage = event.get("usage")
                if not isinstance(usage, dict):
                    print(f"Warning: {location}: turn.completed without valid usage", file=sys.stderr)
                    continue
                counts = {}
                for field in TOKEN_FIELDS:
                    value = usage.get(field, 0)
                    if type(value) is not int or value < 0:
                        raise ValueError(f"{location}: {field} must be a non-negative integer")
                    counts[field] = value
                thread_usage[current_thread_id] = counts
            elif event.get("type") == "step_finish":
                session_id = event.get("sessionID")
                part = event.get("part")
                if not isinstance(session_id, str) or not session_id.strip() or not isinstance(part, dict):
                    print(f"Warning: {location}: step_finish without valid sessionID/part", file=sys.stderr)
                    continue
                message_id = part.get("messageID")
                tokens = part.get("tokens")
                if not isinstance(message_id, str) or not message_id.strip() or not isinstance(tokens, dict):
                    print(f"Warning: {location}: step_finish without valid messageID/tokens", file=sys.stderr)
                    continue
                cache = tokens.get("cache", {})
                if not isinstance(cache, dict):
                    print(f"Warning: {location}: step_finish without valid tokens.cache", file=sys.stderr)
                    continue
                values = {
                    "input": tokens.get("input", 0),
                    "output": tokens.get("output", 0),
                    "reasoning": tokens.get("reasoning", 0),
                    "cache.read": cache.get("read", 0),
                    "cache.write": cache.get("write", 0),
                }
                for field, value in values.items():
                    if type(value) is not int or value < 0:
                        raise ValueError(f"{location}: tokens.{field} must be a non-negative integer")
                # OpenCode 2.3.0 reports disjoint input/output/reasoning/cache
                # categories. Our common format includes cached tokens in input
                # and reasoning in output. Native cost may be zero for OAuth.
                step_usage[(session_id, message_id)] = {
                    "input_tokens": values["input"] + values["cache.read"] + values["cache.write"],
                    "cached_input_tokens": values["cache.read"],
                    "output_tokens": values["output"] + values["reasoning"],
                }

    if opencode_detected:
        print(f"Warning: {log_path}: no {OPENCODE_USAGE_FILENAME}; OpenCode stdout usage is root-only and incomplete "
              "(child sessions and other unlogged usage are not included)", file=sys.stderr)
    if not thread_usage and not step_usage:
        print(f"Warning: {log_path}: no thread usage found; counting zero tokens", file=sys.stderr)
    for thread_id, usage in thread_usage.items():
        if usage is None:
            print(
                f"Warning: {log_path}: no completed-turn usage for thread {thread_id}; counting zero tokens",
                file=sys.stderr,
            )
    return {
        field: sum(usage[field] for usage in thread_usage.values() if usage is not None)
        + sum(usage[field] for usage in step_usage.values())
        for field in TOKEN_FIELDS
    }


def extract_raw_cost_tokens(
    run_folder: str | Path, *, readable: Callable[[Path], bool] | None = None,
) -> list[dict[str, Any]]:
    """Return counts for attempted games. For partial archives, pass a file predicate
    to skip unavailable inputs and warn about individual sessions that fail to parse.
    """
    run_folder = Path(run_folder).expanduser().resolve()
    records = []
    for log_path in game_server_logs(run_folder, required=readable is None):
        if readable is not None and not readable(log_path):
            continue
        game_folder = log_path.parent.parent
        try:
            session = read_session(log_path)
            environment_id = session[1] if session is not None else read_start_attempt(log_path)
            if environment_id is None:
                continue
            counts = extract_token_counts(game_folder / "run" / "agent.log", readable=readable)
        except (OSError, ValueError) as exc:
            if readable is None:
                raise
            print(f"Warning: skipping token counts for {game_folder}: {exc}", file=sys.stderr)
            continue
        records.append({**counts, "environment_id": environment_id, "run_folder": str(game_folder)})
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_folder", type=Path, help="Run directory containing <alias>/server/server.log and per-game folders")
    args = parser.parse_args(argv)

    try:
        run_folder = args.run_folder.expanduser().resolve()
        records = extract_raw_cost_tokens(run_folder)
        output_path = run_folder / "raw_cost_tokens.json"
        output = json.dumps(records, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        output_path.write_text(output, encoding="utf-8")
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Saved token counts from {len(records)} sessions to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
