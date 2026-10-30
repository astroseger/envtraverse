from __future__ import annotations

import io
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_funs as rf


class RunPermissionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.paths = rf.RunPaths(self.directory)
        self.run_dir = self.paths.run_dir
        self.run_dir.mkdir()
        self.enterContext(patch.object(rf, "log_info"))
        self.logger = self.enterContext(patch.object(rf, "LOGGER"))
        self.errors = self.enterContext(patch("sys.stderr", new_callable=io.StringIO))

    def test_recursively_adds_only_owner_write_and_preserves_contents(self) -> None:
        paths = {}
        for name, mode in (
            ("alias/client/.client_session.json", 0o444),
            ("alias/observations/actions/00001.json", 0o400),
            ("alias/observations/final.png", 0o444),
            ("alias/run/agent_run/helper.sh", 0o555),
            ("alias/run/agent_run/.git/objects/data", 0o444),
            ("server/log.txt", 0o660),
        ):
            path = self.run_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"unchanged\x00\xff")
            path.chmod(mode)
            paths[path] = mode
        for path in self.run_dir.rglob("*"):
            if path.is_dir():
                path.chmod(0o555)
                paths[path] = 0o555
        self.run_dir.chmod(0o555)
        paths[self.run_dir] = 0o555

        self.assertIsNone(rf.make_run_writable(self.paths))
        for path, original_mode in paths.items():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), original_mode | stat.S_IWUSR, str(path))
            if path.is_file():
                self.assertEqual(path.read_bytes(), b"unchanged\x00\xff")
        with patch.object(Path, "chmod") as chmod:
            rf.make_run_writable(self.paths)
            chmod.assert_not_called()

    def test_skips_file_directory_and_broken_symlinks(self) -> None:
        outside = self.directory / "outside"
        outside.mkdir()
        target = outside / "file.txt"
        target.write_text("outside", encoding="utf-8")
        target.chmod(0o444)
        outside.chmod(0o555)
        (self.run_dir / "file-link").symlink_to(target)
        (self.run_dir / "directory-link").symlink_to(outside, target_is_directory=True)
        (self.run_dir / "broken-link").symlink_to(self.directory / "missing")
        rf.make_run_writable(self.paths)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o444)
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o555)
        self.logger.warning.assert_not_called()

    def test_symlink_run_root_is_not_followed(self) -> None:
        target = self.run_dir / "file.txt"
        target.write_text("unchanged", encoding="utf-8")
        target.chmod(0o444)
        self.run_dir.chmod(0o555)
        linked_paths = rf.RunPaths(self.directory / "linked experiment")
        linked_paths.root.mkdir()
        link = linked_paths.run_dir
        link.symlink_to(self.run_dir, target_is_directory=True)
        rf.make_run_writable(linked_paths)
        self.assertEqual(stat.S_IMODE(self.run_dir.stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o444)

    def test_missing_run_is_a_noop(self) -> None:
        missing = self.directory / "missing"
        with patch.object(Path, "chmod") as chmod:
            rf.make_run_writable(rf.RunPaths(missing))
            chmod.assert_not_called()
        self.assertFalse(missing.exists())

    def test_permission_failure_is_logged_and_other_files_are_still_restored(self) -> None:
        denied = self.run_dir / "denied.txt"
        normal = self.run_dir / "normal.txt"
        for path in (denied, normal):
            path.write_text("unchanged", encoding="utf-8")
            path.chmod(0o444)
        original_chmod = Path.chmod

        def chmod(path, mode, **kwargs):
            if path == denied:
                raise PermissionError(1, "Operation not permitted", str(path))
            return original_chmod(path, mode, **kwargs)

        with patch.object(Path, "chmod", autospec=True, side_effect=chmod):
            self.assertIsNone(rf.make_run_writable(self.paths))
        self.assertEqual(stat.S_IMODE(denied.stat().st_mode), 0o444)
        self.assertEqual(stat.S_IMODE(normal.stat().st_mode), 0o644)
        self.logger.warning.assert_called_once()
        self.assertIn("denied.txt", self.errors.getvalue())

    def test_directory_walk_errors_are_reported_without_raising(self) -> None:
        def walk(path, *, followlinks, onerror):
            self.assertFalse(followlinks)
            onerror(PermissionError(13, "Permission denied", str(path)))
            return iter(())

        with patch.object(rf.os, "walk", side_effect=walk):
            self.assertIsNone(rf.make_run_writable(self.paths))
        self.logger.warning.assert_called_once()
        self.assertIn("Permission denied", self.errors.getvalue())


if __name__ == "__main__":
    unittest.main()
