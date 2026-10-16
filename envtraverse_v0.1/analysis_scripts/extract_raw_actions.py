#!/usr/bin/env python3
"""Extract per-session action histories from a run's <alias>/server/server.log."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import sys
from typing import Any


if __package__:
    from .server_logs import game_server_logs, read_session, server_messages
else:
    from server_logs import game_server_logs, read_session, server_messages


APPLIED_ACTION = re.compile(r"applied action session=(\S+) action=(.+) step_index=(\d+)")


def extract_raw_actions(run_folder: str | Path) -> list[dict[str, Any]]:
    """Return one record per started game, sorted by alias, with actions in order."""
    run_folder = Path(run_folder).expanduser().resolve()
    records = []
    for log_path in game_server_logs(run_folder):
        session = read_session(log_path)
        actions = []
        started = False
        for location, message in server_messages(log_path):
            if message.startswith("started session "):
                started = True
            elif message.startswith("applied action "):
                match = APPLIED_ACTION.fullmatch(message)
                if match is None:
                    raise ValueError(f"{location}: malformed applied-action entry")
                token, action_text, _step_index = match.groups()
                if not started or session is None or token != session[0]:
                    raise ValueError(f"{location}: action for unknown session token {token}")
                # Server logs Python dict representations. Never eval log content.
                try:
                    action = ast.literal_eval(action_text)
                except (SyntaxError, ValueError) as exc:
                    raise ValueError(f"{location}: invalid action dictionary") from exc
                if not isinstance(action, dict):
                    raise ValueError(f"{location}: action must be a dictionary")
                actions.append(action)
        if session is not None:
            records.append({
                "actions": actions,
                "environment_id": session[1],
                "run_folder": str(log_path.parent.parent),
            })
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_folder", type=Path, help="Run directory containing <alias>/server/server.log")
    args = parser.parse_args(argv)

    try:
        run_folder = args.run_folder.expanduser().resolve()
        records = extract_raw_actions(run_folder)
        output_path = run_folder / "raw_actions.json"
        output = json.dumps(records, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        output_path.write_text(output, encoding="utf-8")
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    action_count = sum(len(record["actions"]) for record in records)
    print(f"Saved {action_count} actions from {len(records)} sessions to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
