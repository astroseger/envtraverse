#!/usr/bin/env python3
"""Extract per-game token counts, including failed setup, without cost estimates."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any

if __package__:
    from .server_logs import game_server_logs, read_session, read_start_attempt
else:
    from server_logs import game_server_logs, read_session, read_start_attempt


TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens")
OMEGA_USAGE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \|\s*INFO\s*\|\s*lib_llm_ext\s*\|\s*"
    r"\[LLM_USAGE\] provider=ChatGPT(?:\s+(?P<fields>.*))?$"
)


def detect_log_format(log_path: Path) -> str:
    """Prefer Codex events, even if Omega usage appears earlier in the log."""
    has_omega_usage = False
    with log_path.open(encoding="utf-8") as log:
        for raw_line in log:
            line = raw_line.strip()
            if line.startswith("{"):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # Leave diagnostics to the selected parser.
                    continue
                if event.get("type") in ("thread.started", "turn.completed"):
                    return "codex"
            if OMEGA_USAGE.fullmatch(raw_line.rstrip("\r\n")) is not None:
                has_omega_usage = True
    return "omega_chatgpt" if has_omega_usage else "unknown"


def extract_omega_chatgpt_token_counts(log_path: Path) -> dict[str, int]:
    """Sum reported per-response usage from Omega's canonical agent.log.

    Cached input and reasoning output are already included in their respective
    totals. Requests without usage reports cannot be reconstructed from this log.
    """
    totals = dict.fromkeys(TOKEN_FIELDS, 0)
    source_fields = ("input_tokens", "cached_tokens", "output_tokens")
    with log_path.open(encoding="utf-8") as log:
        for line_number, raw_line in enumerate(log, start=1):
            match = OMEGA_USAGE.fullmatch(raw_line.rstrip("\r\n"))
            if match is None:
                continue
            location = f"{log_path}:{line_number}"
            fields = {}
            for item in (match.group("fields") or "").split():
                key, separator, value = item.partition("=")
                if not separator or not key or key in fields:
                    raise ValueError(f"{location}: malformed or duplicate Omega usage field {item!r}")
                fields[key] = value
            for source, target in zip(source_fields, TOKEN_FIELDS):
                value = fields.get(source)
                if value is None or value == "None":
                    print(
                        f"Warning: {location}: Omega {source} unavailable; counting zero for this field",
                        file=sys.stderr,
                    )
                    continue
                if re.fullmatch(r"[0-9]+", value) is None:
                    raise ValueError(f"{location}: {source} must be a non-negative integer")
                totals[target] += int(value)
    return totals


def extract_agent_token_counts(log_path: Path) -> dict[str, int]:
    """Dispatch per game; unknown formats retain the existing Codex behavior."""
    if detect_log_format(log_path) == "omega_chatgpt":
        return extract_omega_chatgpt_token_counts(log_path)
    return extract_token_counts(log_path)


def extract_token_counts(log_path: Path) -> dict[str, int]:
    """Sum the latest completed-turn usage per thread, as in the old analyser.

    Repeated thread.started events (resumes/recovery) do not reset a thread's
    counters. Incomplete turns have no usage report and cannot be counted here.
    """
    thread_usage: dict[str, dict[str, int] | None] = {}
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

    if not thread_usage:
        print(f"Warning: {log_path}: no thread usage found; counting zero tokens", file=sys.stderr)
    for thread_id, usage in thread_usage.items():
        if usage is None:
            print(
                f"Warning: {log_path}: no completed-turn usage for thread {thread_id}; counting zero tokens",
                file=sys.stderr,
            )
    return {
        field: sum(usage[field] for usage in thread_usage.values() if usage is not None)
        for field in TOKEN_FIELDS
    }


def extract_raw_cost_tokens(run_folder: str | Path) -> list[dict[str, Any]]:
    """Return token counts for attempted games in alias order, including failed setup."""
    run_folder = Path(run_folder).expanduser().resolve()
    records = []
    for log_path in game_server_logs(run_folder):
        session = read_session(log_path)
        environment_id = session[1] if session is not None else read_start_attempt(log_path)
        if environment_id is None:
            continue
        game_folder = log_path.parent.parent
        records.append({
            **extract_agent_token_counts(game_folder / "run" / "agent.log"),
            "environment_id": environment_id,
            "run_folder": str(game_folder),
        })
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
