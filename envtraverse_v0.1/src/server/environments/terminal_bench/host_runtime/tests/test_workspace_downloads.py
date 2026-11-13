"""Real archive/filesystem tests; Docker tests cover distinct sandbox users."""
from __future__ import annotations

import asyncio
import errno
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tarfile
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from terminal_bench.downloads import EXPORT_SCRIPT, WorkspaceDownloads, receive_export
from terminal_bench.protocol import RuntimeFault, decode_observation
from terminal_bench.uploads import Snapshot


def member(name, data=b"", *, kind=tarfile.REGTYPE, mode=0o640, link=""):
    entry = tarfile.TarInfo(name)
    entry.type, entry.mode, entry.linkname = kind, mode, link
    entry.size = len(data) if kind == tarfile.REGTYPE else 0
    return entry, data


class DownloadFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tb_download_")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "run/session/run/agent_run"
        self.workspace.mkdir(parents=True)

    def archive(self, entries):
        stage = Path(tempfile.mkdtemp(dir=self.root, prefix="stage-"))
        archive = stage / "export.tar"
        with tarfile.open(archive, "w") as stream:
            for entry, data in entries:
                stream.addfile(entry, io.BytesIO(data))
        return archive

    def receive(self, entries, destination="result", *, directory=True, cancelled=None):
        return receive_export(self.archive(entries), self.workspace, destination, directory=directory,
                              root_name="source", cancelled=cancelled or threading.Event())

    def test_directory_merge_preserves_binary_empty_paths_and_ordinary_modes(self):
        (self.workspace / "result").mkdir()
        (self.workspace / "result/keep").write_text("untouched")
        (self.workspace / "result/run.sh").write_text("old")
        data = bytes(range(256))
        result = self.receive([
            member(".", kind=tarfile.DIRTYPE, mode=0o750),
            member("./empty", kind=tarfile.DIRTYPE, mode=0o700),
            member("./nested", kind=tarfile.DIRTYPE, mode=0o755),
            member("./nested/a 'Ω.bin", data),
            member("./run.sh", b"#!/bin/sh\n", mode=0o4751),
            member("./empty-file"),
        ])
        target = self.workspace / "result"
        self.assertEqual((result.directory, result.size, result.files, result.status),
                         (True, len(data) + 10, 3, "success"))
        self.assertEqual((target / "nested/a 'Ω.bin").read_bytes(), data)
        self.assertEqual((target / "keep").read_text(), "untouched")
        self.assertTrue((target / "empty").is_dir())
        self.assertEqual((target / "empty-file").stat().st_size, 0)
        for path, mode in [(target, 0o750), (target / "run.sh", 0o751), (target / "nested/a 'Ω.bin", 0o640)]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
            self.assertEqual(path.stat().st_uid, os.getuid())

    def test_files_and_directory_totals_above_old_limit(self):
        data = b"\0\xff" * (17 * 1024 * 1024)
        file = self.receive([member("source", data)], "large.bin", directory=False)
        self.assertEqual((file.directory, file.size, file.files, file.status),
                         (False, len(data), 1, "success"))
        self.assertEqual((self.workspace / "large.bin").read_bytes(), data)
        result = self.receive([member(".", kind=tarfile.DIRTYPE),
                               member("./a", data[:len(data)//2]), member("./b", data[len(data)//2:])])
        self.assertEqual(result.size, len(data))
        self.assertEqual(result.files, 2)

    def test_merge_into_workspace_retains_its_mode(self):
        self.workspace.chmod(0o750)
        self.receive([member(".", kind=tarfile.DIRTYPE, mode=0o500), member("./a", b"hello")], ".")
        self.assertEqual((self.workspace / "a").read_bytes(), b"hello")
        self.assertEqual(stat.S_IMODE(self.workspace.stat().st_mode), 0o750)

    def test_replacement_does_not_modify_a_hard_link_outside_workspace(self):
        outside = self.root / "outside"
        outside.write_bytes(b"outside")
        os.link(outside, self.workspace / "result")
        self.receive([member("source", b"new")], directory=False)
        self.assertEqual(outside.read_bytes(), b"outside")
        self.assertEqual((self.workspace / "result").read_bytes(), b"new")

    def test_destination_links_special_files_and_missing_parents_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep").write_bytes(b"outside")
        (self.workspace / "link").symlink_to(outside, target_is_directory=True)
        (self.workspace / "file-link").symlink_to(outside / "keep")
        os.mkfifo(self.workspace / "pipe")
        for destination in ("link/keep", "file-link", "pipe", "missing/file", "../outside/keep", "/tmp/outside"):
            with self.subTest(destination=destination), self.assertRaises(RuntimeFault):
                self.receive([member("source", b"new")], destination, directory=False)
        with self.assertRaises(RuntimeFault):
            self.receive([member(".", kind=tarfile.DIRTYPE), member("./keep", b"new")], "link")
        self.assertEqual((outside / "keep").read_bytes(), b"outside")

    def test_workspace_root_cannot_redirect_to_another_session(self):
        self.workspace.rmdir()
        outside = self.root / "other-session"
        outside.mkdir()
        self.workspace.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RuntimeFault):
            self.receive([member("source", b"new")], directory=False)
        self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipIf(os.geteuid() == 0, "Root bypasses ordinary file permissions")
    def test_read_only_destination_is_not_replaced(self):
        target = self.workspace / "result"
        target.write_bytes(b"original")
        target.chmod(0o400)
        with self.assertRaises(RuntimeFault):
            self.receive([member("source", b"new")], directory=False)
        self.assertEqual(target.read_bytes(), b"original")

    def test_malformed_exports_fail_before_changing_workspace(self):
        invalid = [
            member("../escape", b"bad"), member("/absolute", b"bad"),
            member("./link", kind=tarfile.SYMTYPE, link="/outside"),
            member("./hard", kind=tarfile.LNKTYPE, link="./keep"),
            member("./fifo", kind=tarfile.FIFOTYPE), member("./device", kind=tarfile.CHRTYPE),
            member("./keep", b"duplicate"), member("./missing/file", b"unordered"),
        ]
        (self.workspace / "result").mkdir()
        sentinel = self.workspace / "result/keep"
        sentinel.write_bytes(b"original")
        for entry in invalid:
            with self.subTest(name=entry[0].name), self.assertRaises(RuntimeFault):
                self.receive([member(".", kind=tarfile.DIRTYPE), member("./keep", b"replacement"), entry])
            self.assertEqual(sentinel.read_bytes(), b"original")

    def test_file_requires_exact_filename_and_export_shape(self):
        for destination in (".", "result/", "result/."):
            with self.subTest(destination=destination), self.assertRaises(RuntimeFault):
                self.receive([member("source", b"data")], destination, directory=False)
        with self.assertRaises(RuntimeFault):
            self.receive([member("other", b"data")], directory=False)
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_cancellation_before_install_does_not_change_workspace(self):
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(InterruptedError):
            self.receive([member("source", b"data")], directory=False, cancelled=cancelled)
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_partial_merge_reports_committed_files_and_stops_at_first_failure(self):
        target = self.workspace / "result"
        target.mkdir()
        (target / "a").write_bytes(b"original")
        (target / "b").mkdir()
        result = self.receive([
            member(".", kind=tarfile.DIRTYPE), member("./a", b"copied"),
            member("./b", b"blocked"), member("./c", b"not attempted"),
        ])
        self.assertEqual((result.status, result.directory, result.files, result.size,
                          result.files_not_copied), ("partial", True, 1, 6, 2))
        self.assertIn("result/b", result.error)
        self.assertNotIn(str(self.root), result.error)
        self.assertEqual((target / "a").read_bytes(), b"copied")
        self.assertTrue((target / "b").is_dir())
        self.assertFalse((target / "c").exists())
        self.assertEqual({path.name for path in target.iterdir()}, {"a", "b"})

    def test_zero_byte_file_is_progress(self):
        target = self.workspace / "result"
        target.mkdir()
        (target / "b").mkdir()
        result = self.receive([member(".", kind=tarfile.DIRTYPE), member("./a"),
                               member("./b", b"blocked")])
        self.assertEqual((result.status, result.files, result.size, result.files_not_copied),
                         ("partial", 1, 0, 1))
        self.assertEqual((target / "a").read_bytes(), b"")

    def test_created_directory_is_progress_before_first_file_failure(self):
        target = self.workspace / "result"
        target.mkdir()
        (target / "b").mkdir()
        result = self.receive([member(".", kind=tarfile.DIRTYPE),
                               member("./a-empty", kind=tarfile.DIRTYPE, mode=0o700),
                               member("./b", b"blocked")])
        self.assertEqual((result.status, result.files, result.size, result.files_not_copied),
                         ("partial", 0, 0, 1))
        self.assertTrue((target / "a-empty").is_dir())
        self.assertIn("result/b", result.error)

    def test_changed_directory_mode_is_progress_before_first_file_failure(self):
        target = self.workspace / "result"
        (target / "a-empty").mkdir(parents=True)
        (target / "a-empty").chmod(0o755)
        (target / "b").mkdir()
        result = self.receive([member(".", kind=tarfile.DIRTYPE),
                               member("./a-empty", kind=tarfile.DIRTYPE, mode=0o700),
                               member("./b", b"blocked")])
        self.assertEqual((result.status, result.files, result.size, result.files_not_copied),
                         ("partial", 0, 0, 1))
        self.assertEqual(stat.S_IMODE((target / "a-empty").stat().st_mode), 0o700)

    def test_metadata_failure_after_all_files_still_reports_partial(self):
        original_fchmod = os.fchmod

        def fail_directory_mode(fd, mode):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise PermissionError(errno.EACCES, "private host detail: " + str(self.root))
            original_fchmod(fd, mode)

        with patch("terminal_bench.downloads.os.fchmod", side_effect=fail_directory_mode):
            result = self.receive([member(".", kind=tarfile.DIRTYPE, mode=0o750),
                                   member("./data", b"copied")])
        self.assertEqual((result.status, result.files, result.size, result.files_not_copied),
                         ("partial", 1, 6, 0))
        self.assertEqual((self.workspace / "result/data").read_bytes(), b"copied")
        self.assertIn("result", result.error)
        self.assertNotIn(str(self.root), result.error)
        self.assertNotIn("private host detail", result.error)

    def test_failure_without_persistent_progress_is_an_error(self):
        target = self.workspace / "result"
        (target / "a").mkdir(parents=True)
        with self.assertRaises(RuntimeFault):
            self.receive([member(".", kind=tarfile.DIRTYPE), member("./a", b"blocked")])
        self.assertEqual(list((target / "a").iterdir()), [])

        destination = self.workspace / "file"
        destination.write_bytes(b"original")
        with patch("terminal_bench.downloads.os.replace", side_effect=OSError(errno.EIO, "write failed")):
            with self.assertRaises(RuntimeFault):
                self.receive([member("source", b"new")], "file", directory=False)
        self.assertEqual(destination.read_bytes(), b"original")
        self.assertEqual({path.name for path in self.workspace.iterdir()}, {"file", "result"})

    def test_empty_directory_without_changes_is_success(self):
        result = self.receive([member(".", kind=tarfile.DIRTYPE)], ".")
        self.assertEqual((result.status, result.files, result.size, result.files_not_copied),
                         ("success", 0, 0, 0))
        self.assertIsNone(result.error)

    def test_cancellation_after_committed_file_is_not_a_partial_result(self):
        cancelled = threading.Event()
        original_replace = os.replace

        def cancel_after_replace(*args, **kwargs):
            original_replace(*args, **kwargs)
            cancelled.set()

        with patch("terminal_bench.downloads.os.replace", side_effect=cancel_after_replace):
            with self.assertRaises(InterruptedError):
                self.receive([member(".", kind=tarfile.DIRTYPE), member("./a", b"copied"),
                              member("./b", b"not attempted")], cancelled=cancelled)
        self.assertEqual((self.workspace / "result/a").read_bytes(), b"copied")
        self.assertFalse((self.workspace / "result/b").exists())


class LocalExportEnvironment:
    """Run the actual export script locally; no container/privilege simulation."""
    default_user = "task-user"

    def __init__(self):
        self.calls = []
        self.downloaded = []

    async def exec(self, command, user=None, timeout_sec=None):
        self.calls.append((command, user))
        process = await asyncio.create_subprocess_exec("bash", "-c", command,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_sec)
        return SimpleNamespace(return_code=process.returncode, stdout=stdout.decode(), stderr=stderr.decode())

    async def download_file(self, source, target):
        self.downloaded.append((source, target))
        shutil.copyfile(source, target)


class DownloadExportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tb_export_")
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.source = self.root / "source"
        self.source.mkdir()
        self.env = LocalExportEnvironment()
        self.downloads = WorkspaceDownloads(self.env, self.workspace, self.root / "staging")

    async def asyncTearDown(self):
        self.temporary.cleanup()

    def host_cleanup_failure(self):
        original_cleanup = tempfile.TemporaryDirectory.cleanup

        def cleanup_then_fail(directory):
            original_cleanup(directory)
            raise OSError(errno.EACCES, "cleanup failed")

        return patch("terminal_bench.downloads.tempfile.TemporaryDirectory.cleanup",
                     autospec=True, side_effect=cleanup_then_fail)

    async def test_actual_export_file_directory_permissions_and_cleanup(self):
        name = "a 'quote' $(not-a-command) Ω\n.bin"
        data = bytes(range(256))
        (self.source / name).write_bytes(data)
        (self.source / "empty").mkdir()
        os.link(self.source / name, self.source / "hardlink")
        for source, destination in ((self.source / name, "file.bin"), (self.source, "tree")):
            files, terminal = decode_observation(await self.downloads.get(str(source), destination))
            self.assertFalse(terminal)
            self.assertEqual(set(files), {"transfer.json"})
            receipt = json.loads(files["transfer.json"])
            count = 1 if destination == "file.bin" else 2
            self.assertEqual(receipt, {
                "status": "success", "path_local": destination, "path_sandbox": str(source),
                "kind": "file" if count == 1 else "directory",
                "files_copied": count, "bytes_copied": len(data) * count,
            })
        self.assertEqual((self.workspace / "tree" / name).read_bytes(), data)
        self.assertEqual((self.workspace / "file.bin").read_bytes(), data)
        self.assertTrue((self.workspace / "tree/empty").is_dir())
        self.assertTrue(any(EXPORT_SCRIPT in command and user == "task-user" for command, user in self.env.calls))
        self.assertEqual(list((self.root / "staging").iterdir()), [])
        self.assertTrue(all(not Path(source).parent.exists() for source, _ in self.env.downloaded))

    async def test_partial_copy_returns_receipt_and_cleans_staging(self):
        for name, content in (("a", b"copied"), ("b", b"blocked"), ("c", b"not attempted")):
            (self.source / name).write_bytes(content)
        (self.workspace / "result/b").mkdir(parents=True)
        files, terminal = decode_observation(await self.downloads.get(str(self.source), "result"))
        self.assertFalse(terminal)
        self.assertEqual(set(files), {"transfer.json"})
        receipt = json.loads(files["transfer.json"])
        error = receipt.pop("error")
        self.assertIsInstance(error, str)
        self.assertIn("result/b", error)
        self.assertNotIn(str(self.root), error)
        self.assertEqual(receipt, {
            "status": "partial", "path_local": "result", "path_sandbox": str(self.source),
            "kind": "directory", "files_copied": 1, "bytes_copied": 6, "files_not_copied": 2,
        })
        self.assertEqual((self.workspace / "result/a").read_bytes(), b"copied")
        self.assertFalse((self.workspace / "result/c").exists())
        self.assertEqual(list((self.root / "staging").iterdir()), [])
        self.assertTrue(all(not Path(source).parent.exists() for source, _ in self.env.downloaded))

    async def test_copy_without_progress_raises_and_cleans_staging(self):
        (self.source / "a").write_bytes(b"blocked")
        (self.workspace / "result/a").mkdir(parents=True)
        with self.assertRaises(RuntimeFault):
            await self.downloads.get(str(self.source), "result")
        self.assertEqual(list((self.workspace / "result/a").iterdir()), [])
        self.assertEqual(list((self.root / "staging").iterdir()), [])
        self.assertTrue(all(not Path(source).parent.exists() for source, _ in self.env.downloaded))

    async def test_host_cleanup_error_does_not_hide_copy_receipts(self):
        (self.source / "a").write_bytes(b"copied")
        (self.source / "b").write_bytes(b"blocked")
        for status in ("success", "partial"):
            with self.subTest(status=status):
                if status == "partial":
                    (self.workspace / status / "b").mkdir(parents=True)
                with self.host_cleanup_failure():
                    with self.assertLogs("terminal_bench.downloads", level="WARNING") as logged:
                        files, terminal = decode_observation(await self.downloads.get(str(self.source), status))
                self.assertFalse(terminal)
                receipt = json.loads(files["transfer.json"])
                self.assertEqual(receipt["status"], status)
                self.assertEqual(receipt["files_copied"], 2 if status == "success" else 1)
                self.assertEqual((self.workspace / status / "a").read_bytes(), b"copied")
                self.assertTrue(any("Could not clean host download staging directory" in record
                                    for record in logged.output))

    async def test_host_cleanup_error_preserves_original_copy_error(self):
        (self.source / "a").write_bytes(b"blocked")
        (self.workspace / "result/a").mkdir(parents=True)
        with self.host_cleanup_failure():
            with self.assertLogs("terminal_bench.downloads", level="WARNING") as logged:
                with self.assertRaises(RuntimeFault) as failed:
                    await self.downloads.get(str(self.source), "result")
        self.assertEqual(failed.exception.code, "environment_error")
        self.assertIn("result/a", str(failed.exception))
        self.assertNotIn("cleanup failed", str(failed.exception))
        self.assertEqual(list((self.workspace / "result/a").iterdir()), [])
        self.assertTrue(any("Could not clean host download staging directory" in record
                            for record in logged.output))

    async def test_unreadable_missing_links_and_special_sources_do_not_reach_workspace(self):
        real = self.source / "real"
        real.write_bytes(b"content")
        (self.source / "link").symlink_to(real)
        (self.root / "parent-link").symlink_to(self.source, target_is_directory=True)
        os.mkfifo(self.source / "fifo")
        paths = [self.source, self.source / "link", self.source / "fifo", self.source / "missing",
                 self.root / "parent-link/real"]
        if os.geteuid() != 0:
            real.chmod(0o000)
            paths.append(real)
        for path in paths:
            with self.subTest(path=path), self.assertRaises(RuntimeFault):
                await self.downloads.get(str(path), "result")
        self.assertEqual(self.env.downloaded, [])
        self.assertEqual(list(self.workspace.iterdir()), [])

    async def test_cancelled_host_copy_finishes_before_staging_is_removed(self):
        (self.source / "data").write_bytes(b"data")
        entered, finished = threading.Event(), threading.Event()
        def slow_receive(archive, *args, cancelled, **kwargs):
            entered.set()
            if not cancelled.wait(5):
                raise AssertionError("Copy was not cancelled")
            self.assertTrue(archive.is_file())
            finished.set()
            return Snapshot(True, 0, 0)
        with patch("terminal_bench.downloads.receive_export", side_effect=slow_receive):
            task = asyncio.create_task(self.downloads.get(str(self.source), "result"))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(finished.is_set())
        self.assertEqual(list((self.root / "staging").iterdir()), [])
        self.assertEqual(list(self.workspace.iterdir()), [])
