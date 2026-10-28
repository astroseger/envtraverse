from __future__ import annotations

import json

from arcengine import FrameDataRaw


def build_files(frame_data: FrameDataRaw) -> dict[str, str | bytes]:
    data = frame_data.model_dump(mode="json")
    # FrameDataRaw keeps frames in a private attribute excluded from model_dump.
    data["frame"] = [frame.tolist() for frame in frame_data.frame]
    return {"observation.json": json.dumps(data, indent=2) + "\n"}
