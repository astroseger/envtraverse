"""Small atomic, human-readable session records. No database or result deletion."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def session_key(value: Any) -> str:
    """Accept canonical UUID strings or 32 hex digits; store lowercase hex."""
    if not isinstance(value, str):
        raise ValueError("session_id must be a UUID string")
    try:
        result = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("session_id must be a UUID string") from exc
    if value.lower() not in {result.hex, str(result)}:
        raise ValueError("Use 32 hex digits or the canonical hyphenated UUID form")
    return result.hex


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def error_result(directory: Path, *, session_id: str, game_id: str,
                 exception_type: str, note: str, cleanup_required: bool = False,
                 status: str = "error", **identity: Any) -> dict[str, Any]:
    """Called only once the worker is dead, or before it was started."""
    path = directory / "result.json"
    if path.exists():
        return json.loads(path.read_text())
    data = {"session_id": session_id, "game_id": game_id, **identity,
            "status": status, "rewards": None, "exception_type": exception_type,
            "termination_reason": "aborted" if status == "aborted" else "error",
            "verification_status": "unknown",
            "note": note, "cleanup_required": cleanup_required, "finished_at": now()}
    atomic_json(path, data)
    return data
