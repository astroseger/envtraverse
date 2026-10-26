from __future__ import annotations

from io import BytesIO
import json
from typing import Any

import numpy as np
from PIL import Image

COLOR_MAP: dict[int, str] = {
    0: "#FFFFFFFF",
    1: "#CCCCCCFF",
    2: "#999999FF",
    3: "#666666FF",
    4: "#333333FF",
    5: "#000000FF",
    6: "#E53AA3FF",
    7: "#FF7BCCFF",
    8: "#F93C31FF",
    9: "#1E93FFFF",
    10: "#88D8F1FF",
    11: "#FFDC00FF",
    12: "#FF851BFF",
    13: "#921231FF",
    14: "#4FCC30FF",
    15: "#A356D6FF",
}
DEFAULT_PNG_SCALE = 8


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    hex_color = hex_color.lstrip("#")
    return (int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16))


COLOR_LOOKUP = np.array(
    [_hex_to_rgb(COLOR_MAP.get(index, "#000000FF")) for index in range(16)],
    dtype=np.uint8,
)
BLACK_RGB = np.array(_hex_to_rgb("#000000FF"), dtype=np.uint8)


def _frame_to_rgb_array(frame: np.ndarray, scale: int = DEFAULT_PNG_SCALE) -> np.ndarray:
    height, width = frame.shape
    values = frame.astype(np.int64, copy=False)

    rgb_array = np.empty((height, width, 3), dtype=np.uint8)
    rgb_array[...] = BLACK_RGB

    valid = (0 <= values) & (values < len(COLOR_LOOKUP))
    rgb_array[valid] = COLOR_LOOKUP[values[valid]]

    if scale == 1:
        return rgb_array
    return np.repeat(np.repeat(rgb_array, scale, axis=0), scale, axis=1)


def _frame_to_png_bytes(frame: np.ndarray) -> bytes:
    rgb = _frame_to_rgb_array(frame)
    buffer = BytesIO()
    Image.fromarray(rgb, mode="RGB").save(buffer, format="PNG")
    return buffer.getvalue()


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

        files[f"{name_prefix}.png"] = _frame_to_png_bytes(array)

    return files
