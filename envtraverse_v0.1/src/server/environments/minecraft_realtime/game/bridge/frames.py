"""Atomic live-frame writes to a mounted directory, without following links."""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4


class AtomicFrameFile:
    def __init__(self, path: str):
        target = Path(path)
        if not target.is_absolute() or ".." in target.parts or not target.name:
            raise ValueError("LATEST_FRAME_PATH must be an absolute filename")
        self.name = target.name
        self.fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)

    def write(self, jpeg: bytes) -> None:
        temporary = f".{self.name}.{uuid4().hex}.tmp"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o644, dir_fd=self.fd)
            with os.fdopen(fd, "wb") as output:
                os.fchmod(output.fileno(), 0o644)
                output.write(jpeg)
            os.replace(temporary, self.name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def close(self) -> None:
        os.close(self.fd)
