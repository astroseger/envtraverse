"""Validated public environment parameters, shared by adapter and host."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import math
from typing import Any


@dataclass(frozen=True)
class Settings:
    minecraft_task_timeout: float = 1800.0
    difficulty: str = "normal"
    hardcore: bool = False
    seed: int = 123456
    capture_fps: float = 30.0
    record_fps: float = 1.0
    width: int = 960
    height: int = 540
    startup_timeout_seconds: float = 600.0
    render_fps: int = 60

    def __post_init__(self) -> None:
        for name, minimum, maximum in (
            ("minecraft_task_timeout", 0, 7 * 24 * 3600),
            ("startup_timeout_seconds", 0, 3600),
            ("capture_fps", 0, 60),
            ("record_fps", -1, 60),
        ):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not minimum < value <= maximum:
                raise ValueError(f"{name} must be a finite number in ({minimum}, {maximum}]")
        if self.record_fps < 0 or self.record_fps > self.capture_fps:
            raise ValueError("record_fps must be between zero and capture_fps")
        if self.capture_fps < 1:
            raise ValueError("capture_fps must be between 1 and 60")
        for name, minimum, maximum in (("width", 320, 3840), ("height", 240, 2160), ("render_fps", 1, 260)):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
        if self.difficulty not in ("peaceful", "easy", "normal", "hard"):
            raise ValueError("difficulty must be peaceful, easy, normal or hard")
        if type(self.hardcore) is not bool:
            raise ValueError("hardcore must be boolean")
        if self.hardcore and self.difficulty != "hard":
            raise ValueError("hardcore requires difficulty: hard")
        if type(self.seed) is not int or not -(2**63) <= self.seed < 2**63:
            raise ValueError("seed must be a signed 64-bit integer")

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None = None) -> "Settings":
        value = {} if value is None else value
        if not isinstance(value, dict) or set(value) - {field.name for field in fields(cls)}:
            raise ValueError("Unknown life_minecraft settings")
        return cls(**value)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
