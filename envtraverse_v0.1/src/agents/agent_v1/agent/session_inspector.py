from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


# These are the fixed mount points supplied by both run controllers.
CLIENT_DIR = Path("/client")
OBSERVATIONS_DIR = Path("/observations")
CLIENT_SCRIPT_PATH = CLIENT_DIR / "client.py"
CLIENT_STATE_PATH = CLIENT_DIR / ".client_session.json"
ENVIRONMENT_DESCRIPTION_PATH = OBSERVATIONS_DIR / "environment_description.md"


@dataclass(frozen=True)
class SessionInspection:
    terminal: bool
    step_index: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def inspect_sessions() -> SessionInspection:
    if not CLIENT_STATE_PATH.is_file():
        return SessionInspection(terminal=False, step_index=0)

    state = json.loads(CLIENT_STATE_PATH.read_text(encoding="utf-8"))
    return SessionInspection(
        terminal=bool(state.get("terminal", False)),
        step_index=int(state.get("step_index", 0)),
    )
