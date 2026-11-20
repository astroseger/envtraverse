"""Atomic frame publication and append-only private run artifacts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from uuid import uuid4


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=True, allow_nan=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class JsonlLog:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def append(self, value: dict) -> None:
        line = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n"
        with self.lock, self.path.open("a", encoding="utf-8") as output:
            output.write(line)


class JsonlTail:
    """Read only newly completed JSON lines, including across partial writes."""
    def __init__(self, path: Path):
        self.path, self.offset = path, 0

    def read(self) -> list[dict]:
        try:
            with self.path.open("rb") as source:
                if source.seek(0, os.SEEK_END) < self.offset:
                    raise RuntimeError("Event stream was truncated")
                source.seek(self.offset)
                values = []
                while True:
                    line = source.readline()
                    if not line.endswith(b"\n"):
                        break
                    self.offset += len(line)
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("Expected event object")
                    values.append(value)
                return values
        except FileNotFoundError:
            return []


class FramePublisher:
    """Keep the live spectator image and record frames independently of delivery.

    Unless the client publishes directly into a RAM volume, also replace the
    workspace image through a pinned directory descriptor without following links.
    """
    def __init__(self, run_root: Path, session_id: str, directory: Path, record_fps: float, *,
                 publish_to_workspace: bool = True):
        self.fd = None
        if publish_to_workspace:
            self.fd = self._open_workspace(run_root, session_id)
        self.directory = directory
        self.record_fps = record_fps
        self.frames = directory / "frames"
        self.frames.mkdir(exist_ok=True)
        self.manifest = JsonlLog(directory / "frames.jsonl")
        self.latest = None
        self.lock = threading.Lock()
        self.last_recorded = float("-inf")
        self.closed = False

    @staticmethod
    def _open_workspace(run_root: Path, session_id: str) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        current = os.open(run_root, flags)
        try:
            for name in ("run", session_id, "run", "agent_run"):
                child = os.open(name, flags, dir_fd=current)
                os.close(current)
                current = child
            return current
        except BaseException:
            os.close(current)
            raise

    def publish(self, jpeg: bytes, sequence: int, captured_at: float, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
            raise ValueError("Capture is not a complete JPEG")
        with self.lock:
            if self.closed:
                return False
            if self.latest is not None and sequence == self.latest[1]:
                return False
            if self.fd is not None:
                temporary = f".latest_frame.{uuid4().hex}.tmp"
                try:
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o644, dir_fd=self.fd)
                    os.fchmod(fd, 0o644)
                    with os.fdopen(fd, "wb") as output:
                        output.write(jpeg)
                    os.replace(temporary, "latest_frame.jpg", src_dir_fd=self.fd, dst_dir_fd=self.fd)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=self.fd)
                    except FileNotFoundError:
                        pass
            self.latest = (jpeg, sequence, captured_at)
            if self.record_fps > 0 and now - self.last_recorded >= 1 / self.record_fps:
                filename = f"{sequence:012d}.jpg"
                (self.frames / filename).write_bytes(jpeg)
                self.manifest.append({"sequence": sequence, "captured_at": captured_at,
                                      "published_at": time.time(), "file": "frames/" + filename})
                self.last_recorded = now
            return True

    def frame(self):
        with self.lock:
            return self.latest

    def close(self) -> None:
        with self.lock:
            if not self.closed:
                self.closed = True
                if self.fd is not None:
                    os.close(self.fd)
