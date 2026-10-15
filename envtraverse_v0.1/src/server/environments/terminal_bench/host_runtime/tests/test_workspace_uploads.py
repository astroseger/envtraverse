"""Real host filesystem validation; sandbox calls are checked separately in Docker."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import stat
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from terminal_bench.protocol import RuntimeFault
from terminal_bench.uploads import snapshot_workspace, WorkspaceUploads


class WorkspaceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="upload-source-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "run/session/run/agent_run"
        self.workspace.mkdir(parents=True)
        self.target = self.root / "snapshot"

    def snapshot(self, path):
        return snapshot_workspace(self.workspace, path, self.target, threading.Event())

    def test_nested_binary_empty_directories_and_modes(self):
        tree = self.workspace / "source"
        (tree / "empty").mkdir(parents=True)
        (tree / "nested").mkdir()
        binary = tree / "nested/a 'Ω.bin"
        binary.write_bytes(bytes(range(256)))
        binary.chmod(0o640)
        script = tree / "run.sh"
        script.write_bytes(b"#!/bin/sh\necho yes\n")
        script.chmod(0o751)
        tree.chmod(0o750)
        result = self.snapshot("source")
        self.assertTrue(result.directory)
        self.assertEqual(result.files, 2)
        self.assertEqual(result.size, 256 + len(script.read_bytes()))
        self.assertEqual((self.target / "nested/a 'Ω.bin").read_bytes(), bytes(range(256)))
        self.assertTrue((self.target / "empty").is_dir())
        for path, expected in [(self.target, 0o750), (self.target / "run.sh", 0o751),
                               (self.target / "nested/a 'Ω.bin", 0o640)]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), expected)

    def test_file_above_32_mib_is_accepted(self):
        data = bytes(range(256)) * (33 * 4096)
        (self.workspace / "data").write_bytes(data)
        result = self.snapshot("data")
        self.assertFalse(result.directory)
        self.assertEqual(result.size, len(data))
        self.assertEqual(self.target.read_bytes(), data)

    def test_directory_total_above_32_mib_is_accepted(self):
        data = b"x" * (17 * 1024 * 1024)
        for name in ("a", "b"):
            (self.workspace / name).write_bytes(data)
        result = self.snapshot(".")
        self.assertTrue(result.directory)
        self.assertEqual(result.files, 2)
        self.assertEqual(result.size, 2 * len(data))
        for name in ("a", "b"):
            self.assertEqual((self.target / name).read_bytes(), data)

    def test_no_symlinks_even_when_they_point_inside_workspace(self):
        (self.workspace / "real").write_bytes(b"data")
        (self.workspace / "link").symlink_to("real")
        with self.assertRaises(RuntimeFault):
            self.snapshot("link")

    def test_symlinked_parent_cannot_read_other_session(self):
        other = self.root / "run/other/run/agent_run"
        other.mkdir(parents=True)
        (other / "secret").write_text("other session")
        (self.workspace / "outside").symlink_to(other, target_is_directory=True)
        with self.assertRaises(RuntimeFault):
            self.snapshot("outside/secret")
        self.assertFalse(self.target.exists())

    def test_workspace_itself_cannot_be_replaced_with_a_symlink(self):
        self.workspace.rmdir()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "secret").write_text("private host file")
        self.workspace.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(RuntimeFault):
            self.snapshot("secret")
        self.assertFalse(self.target.exists())

    def test_nested_symlinks_and_fifos_are_rejected(self):
        (self.workspace / "link").symlink_to(self.root)
        with self.assertRaises(RuntimeFault):
            self.snapshot(".")
        (self.workspace / "link").unlink()
        os.mkfifo(self.workspace / "pipe")
        # Opening the FIFO must not wait for a writer.
        with self.assertRaises(RuntimeFault):
            snapshot_workspace(self.workspace, "pipe", self.root / "fifo-copy", threading.Event())

    def test_parent_traversal_rejected_before_host_read(self):
        with self.assertRaises(RuntimeFault):
            self.snapshot("../private")
        self.assertFalse(self.target.exists())


class UploadCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_snapshot_finishes_before_cleanup_and_never_uploads(self):
        entered = threading.Event()
        finished = threading.Event()
        targets = []
        def slow_snapshot(workspace, relative, target, cancelled):
            targets.append(target)
            entered.set()
            if not cancelled.wait(5):
                raise AssertionError("Snapshot never received cancellation")
            target.write_bytes(b"final thread write")
            finished.set()
            raise InterruptedError("cancelled")

        uploads = WorkspaceUploads(SimpleNamespace(default_user=None), Path("/unused"), Path(tempfile.gettempdir()))
        with patch("terminal_bench.uploads.snapshot_workspace", side_effect=slow_snapshot):
            pending = asyncio.create_task(uploads.send("source", "/app/destination"))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        self.assertTrue(finished.is_set())
        self.assertFalse(targets[0].parent.exists())

    async def test_invalid_source_does_not_call_sandbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            uploads = WorkspaceUploads(SimpleNamespace(default_user=None), Path(temporary), Path(temporary) / "uploads")
            with self.assertRaises(RuntimeFault):
                await uploads.send("missing", "/app/file")
