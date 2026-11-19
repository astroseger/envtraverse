import errno
import os

import pytest

from life_minecraft.game.bridge.frames import AtomicFrameFile


def test_open_reader_keeps_complete_previous_frame(tmp_path):
    target = tmp_path / "latest_frame.jpg"
    writer = AtomicFrameFile(str(target))
    try:
        writer.write(b"old JPEG")
        with target.open("rb") as previous:
            writer.write(b"new JPEG")
            assert previous.read() == b"old JPEG"
        assert target.read_bytes() == b"new JPEG"
        assert target.stat().st_mode & 0o777 == 0o644
        assert list(tmp_path.iterdir()) == [target]
    finally:
        writer.close()


def test_replace_failure_keeps_old_frame_and_removes_temporary(tmp_path, monkeypatch):
    target = tmp_path / "latest_frame.jpg"
    writer = AtomicFrameFile(str(target))
    try:
        writer.write(b"previous")
        def full(*args, **kwargs):
            raise OSError(errno.ENOSPC, "No space left on device")
        monkeypatch.setattr(os, "replace", full)
        with pytest.raises(OSError, match="No space"):
            writer.write(b"next")
        assert target.read_bytes() == b"previous"
        assert list(tmp_path.iterdir()) == [target]
    finally:
        writer.close()


def test_replaces_agent_symlink_without_writing_its_target(tmp_path):
    target = tmp_path / "latest_frame.jpg"
    outside = tmp_path / "untouched"
    outside.write_bytes(b"keep")
    target.symlink_to(outside)
    writer = AtomicFrameFile(str(target))
    try:
        writer.write(b"JPEG")
        assert outside.read_bytes() == b"keep"
        assert not target.is_symlink()
        assert target.read_bytes() == b"JPEG"
    finally:
        writer.close()


def test_directory_descriptor_survives_replacement(tmp_path):
    directory = tmp_path / "mount"
    directory.mkdir()
    writer = AtomicFrameFile(str(directory / "latest_frame.jpg"))
    try:
        moved = tmp_path / "moved"
        directory.rename(moved)
        directory.symlink_to(tmp_path, target_is_directory=True)
        writer.write(b"JPEG")
        assert (moved / "latest_frame.jpg").read_bytes() == b"JPEG"
        assert not (tmp_path / "latest_frame.jpg").exists()
    finally:
        writer.close()
