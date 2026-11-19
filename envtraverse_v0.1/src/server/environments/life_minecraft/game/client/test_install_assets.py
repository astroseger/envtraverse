"""Image preparation copies only immutable assets and marks successful versions."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from . import install_assets


def layout(root):
    for directory in ("assets/indexes", "libraries", "versions/1.21.1"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / "assets/indexes/17.json").write_text("{}")
    (root / "versions/1.21.1/1.21.1.json").write_text("{}")
    (root / "versions/1.21.1/1.21.1.jar").write_bytes(b"jar")


class ImageAssetsTests(unittest.TestCase):
    def test_download_retry_copies_only_assets_and_writes_ready_marker(self):
        calls = []
        def installer(command, check):
            self.assertFalse(check)
            self.assertIn("--dry", command)
            self.assertIn("--jvm", command)
            self.assertNotIn("--auth", command)
            calls.append(command)
            if len(calls) == 1:
                return SimpleNamespace(returncode=1)
            source = Path(command[command.index("--main-dir") + 1])
            layout(source)
            for excluded in ("options.txt", "portablemc_msa.json", "launcher.log"):
                (source / excluded).write_text("excluded")
            (source / "saves").mkdir()
            return SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(install_assets.subprocess, "run", side_effect=installer), \
             patch.object(install_assets.time, "sleep") as sleep:
            destination = Path(directory) / "minecraft"
            install_assets.install(destination)
            self.assertEqual(len(calls), 2)
            sleep.assert_called_once_with(1)
            self.assertTrue(install_assets.cached_assets_ready(destination))
            self.assertEqual({p.name for p in destination.iterdir()},
                             {"assets", "libraries", "versions", install_assets.STAMP_NAME})

    def test_exhausted_download_never_writes_ready_marker(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(install_assets.subprocess, "run", return_value=SimpleNamespace(returncode=7)) as run, \
             patch.object(install_assets.time, "sleep"):
            destination = Path(directory) / "minecraft"
            with self.assertRaisesRegex(RuntimeError, "failed after 3 attempts"):
                install_assets.install(destination)
            self.assertEqual(run.call_count, 3)
            self.assertFalse(install_assets.cached_assets_ready(destination))

    def test_mismatched_version_missing_files_and_invalid_marker_are_not_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            layout(root)
            marker = root / install_assets.STAMP_NAME
            marker.write_text(json.dumps({**install_assets.STAMP, "minecraft_version": "old"}))
            self.assertFalse(install_assets.cached_assets_ready(root))
            marker.write_text("invalid json")
            self.assertFalse(install_assets.cached_assets_ready(root))
            marker.write_text(json.dumps(install_assets.STAMP))
            self.assertTrue(install_assets.cached_assets_ready(root))
            (root / "versions/1.21.1/1.21.1.jar").unlink()
            self.assertFalse(install_assets.cached_assets_ready(root))
