from __future__ import annotations

import json
from typing import Any

import numpy as np


def _frame_to_ascii(frame: np.ndarray) -> str:
    return "\n".join("".join(format(int(value), "X") for value in row) for row in frame) + "\n"


def _serialize_action_input(frame_data: Any) -> dict[str, Any] | None:
    if frame_data.action_input is None:
        return None
    return {
        "id": frame_data.action_input.id.name
        if hasattr(frame_data.action_input.id, "name")
        else str(frame_data.action_input.id),
        "data": frame_data.action_input.data,
    }


def _serialize_available_action(action: Any) -> Any:
    if isinstance(action, int):
        return f"ACTION{action}"
    return action


def _metadata_for_frame(frame_data: Any, action_input_override: dict[str, Any] | None = None) -> dict[str, Any]:
    action_input = _serialize_action_input(frame_data)
    if action_input is None:
        action_input = action_input_override
    return {
        "state": frame_data.state.name if hasattr(frame_data.state, "name") else str(frame_data.state),
        "levels_completed": frame_data.levels_completed,
        "win_levels": frame_data.win_levels,
        "available_actions": [_serialize_available_action(action) for action in frame_data.available_actions],
        "action_input": action_input,
    }


def build_files(frame_data: Any, action_input_override: dict[str, Any] | None = None) -> dict[str, str | bytes]:
    metadata = _metadata_for_frame(frame_data, action_input_override)
    files: dict[str, str | bytes] = {"metadata.txt": json.dumps(metadata, indent=2)}

    frames = frame_data.frame or []
    for frame_index, frame in enumerate(frames):
        array = np.array(frame, dtype=np.int16)
        if frame_index == len(frames) - 1:
            name_prefix = "final_frame"
        else:
            name_prefix = f"intermediate_frame_{frame_index:02d}"

        files[f"{name_prefix}.txt"] = _frame_to_ascii(array)

    return files
