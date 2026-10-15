from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import analyse_funs


SERVER_LOG_RELATIVE_PATH = Path("server") / "server.log"
STARTED_SESSION_RE = re.compile(
    r"\benv-server\.server: started session "
    r"token=(?P<session>\S+) "
    r"requested_game_id=\S+ "
    r"game_id=(?P<game>\S+)\b"
)
APPLIED_ACTION_RE = re.compile(
    r"\benv-server\.server: applied action "
    r"session=(?P<session>\S+) "
    r"action=(?P<action>.*?) "
    r"step_index=(?P<step_index>\d+)\b"
)


@dataclass(frozen=True)
class ServerAction:
    step_index: int
    action_input: dict[str, Any]
    line_number: int


def parse_logged_action(
    action_text: str,
    server_log_path: Path,
    line_number: int,
) -> dict[str, Any]:
    try:
        action_input = ast.literal_eval(action_text)
    except (SyntaxError, ValueError) as exc:
        raise ValueError(
            f"Invalid action literal in {server_log_path}:{line_number}: {exc}"
        ) from exc

    if not isinstance(action_input, dict):
        raise ValueError(
            f"Expected action object in {server_log_path}:{line_number}"
        )

    action_id = action_input.get("id")
    if not isinstance(action_id, str) or not action_id:
        raise ValueError(
            f"Missing action id in {server_log_path}:{line_number}"
        )

    data = action_input.get("data")
    if data is not None and not isinstance(data, dict):
        raise ValueError(
            f"Expected action data object/null in {server_log_path}:{line_number}"
        )

    return dict(action_input)


def read_server_log(
    server_log_path: Path,
) -> tuple[dict[str, str], dict[str, list[ServerAction]]]:
    if not server_log_path.is_file():
        raise FileNotFoundError(f"Missing server log: {server_log_path}")

    session_by_game: dict[str, str] = {}
    actions_by_session: dict[str, list[ServerAction]] = {}
    with server_log_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            session_match = STARTED_SESSION_RE.search(line)
            if session_match is not None:
                game = session_match.group("game").casefold()
                session = session_match.group("session")
                if game in session_by_game:
                    raise ValueError(
                        f"Duplicate session for game {game!r} in "
                        f"{server_log_path}:{line_number}"
                    )
                session_by_game[game] = session
                continue

            action_match = APPLIED_ACTION_RE.search(line)
            if action_match is None:
                continue

            session = action_match.group("session")
            actions_by_session.setdefault(session, []).append(
                ServerAction(
                    step_index=int(action_match.group("step_index")),
                    action_input=parse_logged_action(
                        action_match.group("action"),
                        server_log_path,
                        line_number,
                    ),
                    line_number=line_number,
                )
            )

    for actions in actions_by_session.values():
        actions.sort(key=lambda action: (action.step_index, action.line_number))
    return session_by_game, actions_by_session


def verify_consecutive_server_steps(
    game_name: str,
    session_token: str,
    server_actions: list[ServerAction],
) -> list[str]:
    errors: list[str] = []
    seen_steps: dict[int, ServerAction] = {}
    for action in server_actions:
        existing = seen_steps.get(action.step_index)
        if existing is not None:
            errors.append(
                f"{game_name}: duplicate server step_index={action.step_index} "
                f"session={session_token} "
                f"lines={existing.line_number},{action.line_number}"
            )
        seen_steps[action.step_index] = action

    expected_steps = list(range(1, len(server_actions) + 1))
    actual_steps = [action.step_index for action in server_actions]
    if actual_steps != expected_steps:
        missing = sorted(set(expected_steps) - set(actual_steps))
        unexpected = sorted(set(actual_steps) - set(expected_steps))
        details = []
        if missing:
            details.append(f"missing={missing[:20]}")
        if unexpected:
            details.append(f"unexpected={unexpected[:20]}")
        details_text = f" ({'; '.join(details)})" if details else ""
        errors.append(
            f"{game_name}: server step_index sequence is not 1..{len(server_actions)} "
            f"session={session_token}{details_text}"
        )

    return errors


def process_game(
    game_dir: Path,
    runs_dir: Path,
    session_by_game: dict[str, str],
    actions_by_session: dict[str, list[ServerAction]],
) -> tuple[Path, int]:
    game_id = analyse_funs.parse_run_folder_name(game_dir.name).game_id
    session_token = session_by_game.get(game_id)
    if session_token is None:
        raise ValueError(
            f"{game_dir.name}: no server session found for game_id={game_id}"
        )

    server_actions = actions_by_session.get(session_token, [])
    if not server_actions:
        raise ValueError(
            f"{game_dir.name}: no accepted server actions for session={session_token}"
        )

    errors = verify_consecutive_server_steps(
        game_dir.name,
        session_token,
        server_actions,
    )
    if errors:
        raise ValueError("; ".join(errors))

    output_path = runs_dir / f"{game_dir.name}_actions.json"
    output_path.write_text(
        json.dumps(
            [action.action_input for action in server_actions],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return output_path, len(server_actions)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract accepted per-game actions from server/server.log."
    )
    parser.add_argument(
        "runs_dir",
        type=Path,
        help="Folder containing server/server.log and game folders",
    )
    args = parser.parse_args()

    runs_dir = args.runs_dir.resolve()
    if not runs_dir.is_dir():
        print(f"Runs folder does not exist: {runs_dir}", file=sys.stderr)
        return 1

    try:
        session_by_game, actions_by_session = read_server_log(
            runs_dir / SERVER_LOG_RELATIVE_PATH
        )
        games = analyse_funs.game_folders(runs_dir)
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not games:
        print(f"No game folders found in {runs_dir}", file=sys.stderr)
        return 1

    failed = False
    for game_dir in games:
        try:
            output_path, action_count = process_game(
                game_dir,
                runs_dir,
                session_by_game,
                actions_by_session,
            )
        except (OSError, ValueError) as exc:
            failed = True
            print(f"ERROR: {exc}", file=sys.stderr)
            continue

        print(
            f"{game_dir.name}: wrote {output_path.name} "
            f"actions={action_count}"
        )

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
