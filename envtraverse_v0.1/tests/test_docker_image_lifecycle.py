"""Opt-in Docker test: TEST_DOCKER_IMAGES=1 python3 -m unittest discover -s tests -p test_docker_image_lifecycle.py -v."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_funs as rf


@unittest.skipUnless(os.environ.get("TEST_DOCKER_IMAGES") == "1", "opt-in: builds Docker images")
class DockerImageLifecycleTest(unittest.TestCase):
    def docker(self, *args):
        return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=60)

    def test_overlapping_builds_and_cleanup_preserve_the_other_runs_images(self) -> None:
        # No account directories, credentials, or authenticated Codex calls are used.
        with tempfile.TemporaryDirectory(prefix="unified-image-test-") as directory:
            first_paths = rf.RunPaths(Path(directory) / "first")
            second_paths = rf.RunPaths(Path(directory) / "second")
            first_dir, second_dir = first_paths.run_dir, second_paths.run_dir
            first_dir.mkdir(parents=True)
            second_dir.mkdir(parents=True)
            with rf.built_run_images("example", "agent_v1", paths=first_paths) as first:
                container = "unified-image-test-" + uuid4().hex
                self.docker(
                    "run", "--rm", "--pull=never", "--network", "none", "--detach",
                    "--name", container, "--entrypoint", "/bin/sleep", first.agent, "600",
                )
                try:
                    with rf.built_run_images("example", "agent_v1", paths=second_paths) as second:
                        self.assertTrue(set(asdict(first).values()).isdisjoint(asdict(second).values()))
                        for image in (*asdict(first).values(), *asdict(second).values()):
                            self.assertTrue(rf.image_exists(image), image)
                        self.assertEqual(self.docker("inspect", "--format", "{{.State.Running}}", container).stdout.strip(), "true")
                        self.assertEqual(self.docker(
                            "run", "--rm", "--pull=never", "--network", "none", first.agent,
                            "python3", "-c", "print('original image still starts new jobs')",
                        ).stdout.strip(), "original image still starts new jobs")
                        self.docker("run", "--rm", "--pull=never", "--network", "none", first.server, "python", "server/server.py", "--help")
                        self.docker("run", "--rm", "--pull=never", "--network", "none", first.proxy, "squid", "-k", "parse")
                    for image in asdict(second).values():
                        self.assertFalse(rf.image_exists(image), image)
                    for image in asdict(first).values():
                        self.assertTrue(rf.image_exists(image), image)
                    self.assertEqual(self.docker("inspect", "--format", "{{.State.Running}}", container).stdout.strip(), "true")
                    self.docker("run", "--rm", "--pull=never", "--network", "none", first.agent, "python3", "--version")
                finally:
                    self.docker("stop", "--timeout", "1", container)
            for image in asdict(first).values():
                self.assertFalse(rf.image_exists(image), image)
            self.assertEqual(json.loads((first_dir / "docker_images.json").read_text()), asdict(first))
            self.assertEqual(json.loads((second_dir / "docker_images.json").read_text()), asdict(second))


if __name__ == "__main__":
    unittest.main()
