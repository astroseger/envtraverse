"""Optional offline Docker check using a synthetic account, never credentials."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.account_locks import AccountLockError, account_lock, ensure_account_not_in_use
from src.controller import run_funs as rf


@unittest.skipUnless(os.environ.get("ACCOUNT_LOCK_DOCKER_TESTS") == "1", "optional offline Docker test")
class AccountLockDockerTest(unittest.TestCase):
    def test_surviving_container_blocks_account_until_real_container_is_stopped(self):
        with tempfile.TemporaryDirectory(prefix="account-lock-smoke-") as directory:
            account = Path(directory) / "synthetic-account"
            account.mkdir()
            name = "account-lock-smoke-" + uuid.uuid4().hex
            self.addCleanup(subprocess.run, ["docker", "rm", "--force", name],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=30)
            image = os.environ.get("ACCOUNT_LOCK_TEST_IMAGE", "python:3.13-slim")
            process = subprocess.Popen(
                ["docker", "run", "--detach", "--rm", "--init", "--pull=never", "--network", "none",
                 "--name", name, "--mount", f"type=bind,source={account},target=/account",
                 image, "sleep", "120"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertTrue(stdout.strip())
            # The client has exited, but the actual container still owns the bind.
            for kind in ("codex", "opencode"):
                with account_lock(account, kind=kind):
                    with self.assertRaisesRegex(AccountLockError, name):
                        ensure_account_not_in_use(account)
            rf.stop_container(name, process, "synthetic account smoke container", timeout=2)
            with account_lock(account, kind="codex"):
                ensure_account_not_in_use(account)
            self.assertFalse(rf.container_exists(name))


if __name__ == "__main__":
    unittest.main()
