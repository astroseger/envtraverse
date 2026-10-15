from __future__ import annotations

from contextlib import ExitStack
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.agents.agent_v1.agent.client.client_storage import (
    CLIENT_CONFIG_FILENAME, ClientConfig, initialize_client_storage, read_client_config,
)


class ClientConfigurationTest(unittest.TestCase):
    def setUp(self) -> None:
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.client_dir = self.root / "client"
        self.observations = self.root / "observations"
        self.client_dir.mkdir()
        self.observations.mkdir()
        self.config_path = self.client_dir / CLIENT_CONFIG_FILENAME

    def test_native_setup_publishes_readonly_configuration_and_storage(self) -> None:
        initialize_client_storage(self.client_dir, self.observations, server_url="http://localhost:12345/")
        self.assertEqual(read_client_config(self.client_dir).to_dict(), {
            "server_url": "http://localhost:12345", "observations_path": str(self.observations),
        })
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o444)
        self.assertEqual(stat.S_IMODE(self.client_dir.stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE(self.observations.stat().st_mode), 0o555)
        self.assertEqual(list(self.observations.iterdir()), [])

    def test_container_path_is_not_resolved_against_host_filesystem(self) -> None:
        with patch.object(Path, "resolve", side_effect=AssertionError("must not resolve a container path on the host")):
            initialize_client_storage(
                self.client_dir, self.observations,
                server_url="http://env-server:8879", observations_path="/observations",
            )
        self.assertEqual(read_client_config(self.client_dir).observations_path, Path("/observations"))

    def test_existing_configuration_is_never_overwritten_during_setup(self) -> None:
        initialize_client_storage(self.client_dir, self.observations, server_url="http://env-server:8879")
        before = self.config_path.read_bytes()
        lock_inode = (self.client_dir / ".client.lock").stat().st_ino
        with self.assertRaisesRegex(FileExistsError, "configuration already exists"):
            initialize_client_storage(self.client_dir, self.observations, server_url="http://another-server:1234")
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual((self.client_dir / ".client.lock").stat().st_ino, lock_inode)

    def test_invalid_setup_is_rejected_before_creating_files(self) -> None:
        with self.assertRaisesRegex(ValueError, "server_url"):
            initialize_client_storage(self.client_dir, self.observations, server_url="not-a-url")
        self.assertEqual(list(self.client_dir.iterdir()), [])
        self.assertEqual(list(self.observations.iterdir()), [])

    def test_configuration_requires_exactly_the_two_settings(self) -> None:
        for value in (None, [], "config", {}, {"server_url": "http://server"}, {
            "server_url": "http://server", "observations_path": "/observations", "unexpected": 1,
        }):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "exactly"):
                ClientConfig.from_dict(value)

    def test_configuration_rejects_invalid_server_urls(self) -> None:
        for url in (
            None, 8879, "", "localhost:8879", "/relative", "file:///observations", "ftp://server",
            "http://", "http://server:bad", "http://server:0", "http://server:65536",
            "http://user:password@server", "http://server?query=1", "http://server#fragment",
            "http://server\n", "http://[invalid",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "server_url"):
                ClientConfig.from_dict({"server_url": url, "observations_path": "/observations"})

    def test_configuration_rejects_relative_or_invalid_observation_paths(self) -> None:
        for path in (None, [], "", ".", "observations", "~/observations", "/bad\0path"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "observations_path"):
                ClientConfig.from_dict({"server_url": "http://server:8879", "observations_path": path})

    def test_read_configuration_reports_missing_invalid_and_unreadable_files(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "configuration is missing"):
            read_client_config(self.client_dir)
        for contents in (b"{", b"\xff", json.dumps({"server_url": "http://server"}).encode("utf-8")):
            self.config_path.write_bytes(contents)
            with self.assertRaisesRegex(RuntimeError, "Invalid client configuration"):
                read_client_config(self.client_dir)
        self.config_path.unlink()
        self.config_path.mkdir()
        with self.assertRaisesRegex(RuntimeError, "Cannot read client configuration"):
            read_client_config(self.client_dir)


if __name__ == "__main__":
    unittest.main()
