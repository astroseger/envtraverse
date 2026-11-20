"""Render FPS settings for the pinned Minecraft client."""
import os


def validate_render_fps(value: object) -> int:
    if type(value) is not int or value not in range(10, 261, 10):
        raise ValueError("render_fps must be an integer multiple of 10 from 10 to 260 (260 means unlimited)")
    return value


def configured_render_fps() -> int:
    try:
        value = int(os.getenv("RENDER_FPS", "30"))
    except ValueError:
        raise ValueError("RENDER_FPS must be an integer") from None
    return validate_render_fps(value)
