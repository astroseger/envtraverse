from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TerminalSettings:
    columns: int = 160
    rows: int = 40
    history_limit: int = 10000
    max_file_bytes: int = 32 * 1024 * 1024
    install_tmux: bool = True

    def __post_init__(self) -> None:
        limits = {
            "columns": (20, 300), "rows": (5, 120),
            "history_limit": (2000, 100000),
            "max_file_bytes": (1, 32 * 1024 * 1024),
        }
        for name, (minimum, maximum) in limits.items():
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
        if type(self.install_tmux) is not bool:
            raise ValueError("install_tmux must be boolean")
