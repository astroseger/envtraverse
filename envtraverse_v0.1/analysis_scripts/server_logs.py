"""Read game starts and sessions from each <alias>/server/server.log."""
from __future__ import annotations

from pathlib import Path
import re
from collections.abc import Iterator


SERVER_MESSAGE = re.compile(r"^\S+ \S+ \[[^\]]+\] env-server\.server: (.*)$")
SESSION_START = re.compile(
    r"started session token=(\S+) requested_game_id=(\S+) game_id=(.+)"
)
START_ATTEMPT = re.compile(r"starting requested_game_id=(\S+) game_id=(.+)")


def game_server_logs(run_folder: Path, *, required: bool = True) -> list[Path]:
    paths = sorted(run_folder.glob("*/server/server.log"))
    if required and not paths:
        raise FileNotFoundError(f"No per-game server logs found: {run_folder}/*/server/server.log")
    return paths


def server_messages(log_path: Path) -> Iterator[tuple[str, str]]:
    with log_path.open(encoding="utf-8") as log:
        for line_number, line in enumerate(log, start=1):
            match = SERVER_MESSAGE.fullmatch(line.rstrip("\r\n"))
            if match is not None:
                yield f"{log_path}:{line_number}", match.group(1)


def read_start_attempt(log_path: Path) -> str | None:
    """Return the real game ID for a start attempt, including failed setup."""
    game_id = None
    for location, message in server_messages(log_path):
        if not message.startswith("starting requested_game_id"):
            continue
        match = START_ATTEMPT.fullmatch(message)
        if match is None:
            raise ValueError(f"{location}: malformed start-attempt entry")
        alias, game = match.groups()
        if Path(alias).name != alias or alias in {".", ".."}:
            raise ValueError(f"{location}: requested_game_id must be a folder name")
        if alias != log_path.parent.parent.name:
            raise ValueError(f"{location}: requested_game_id does not match its game folder")
        if game_id is not None:
            raise ValueError(f"{location}: multiple start attempts in single-game server log")
        game_id = game
    return game_id


def read_session(log_path: Path) -> tuple[str, str] | None:
    """Return (session token, real game ID), or None if the game never started."""
    session = None
    for location, message in server_messages(log_path):
        if not message.startswith("started session "):
            continue
        match = SESSION_START.fullmatch(message)
        if match is None:
            raise ValueError(f"{location}: malformed session-start entry")
        token, alias, game = match.groups()
        if Path(alias).name != alias or alias in {".", ".."}:
            raise ValueError(f"{location}: requested_game_id must be a folder name")
        if alias != log_path.parent.parent.name:
            raise ValueError(f"{location}: requested_game_id does not match its game folder")
        if session is not None:
            raise ValueError(f"{location}: multiple sessions in single-game server log")
        session = token, game
    return session
