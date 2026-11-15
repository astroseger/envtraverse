from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import account_locks as locks


def result(stdout="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class AccountLocksTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.account = self.root / "account with spaces"
        self.account.mkdir()

    def test_each_kind_is_nonblocking_and_releases_on_exit(self):
        for kind in ("codex", "opencode"):
            with self.subTest(kind=kind):
                with locks.account_lock(self.account, kind=kind) as fd:
                    path = self.root / f".{self.account.name}.{kind}.lock"
                    self.assertEqual(os.fstat(fd).st_ino, path.stat().st_ino)
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    with self.assertRaisesRegex(locks.AccountLockError, "Account is in use"):
                        with locks.account_lock(self.account, kind=kind):
                            self.fail("same account was acquired twice")
                with locks.account_lock(self.account, kind=kind):
                    pass
                self.assertTrue(path.is_file())

    def test_borrowed_descriptor_remains_open_and_locked_even_after_exception(self):
        path = self.root / f".{self.account.name}.codex.lock"
        with path.open("a") as owner:
            with self.assertRaisesRegex(RuntimeError, "test body"):
                with locks.account_lock(self.account, kind="codex", inherited_fd=owner.fileno()) as fd:
                    self.assertEqual(fd, owner.fileno())
                    raise RuntimeError("test body")
            os.fstat(owner.fileno())
            with path.open("a") as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_borrowed_descriptor_must_match_regular_lock_inode(self):
        path = self.root / f".{self.account.name}.codex.lock"
        with (self.root / "wrong").open("a") as wrong:
            path.touch()
            with self.assertRaises(locks.AccountLockError):
                with locks.account_lock(self.account, kind="codex", inherited_fd=wrong.fileno()):
                    pass
            path.unlink()
            path.symlink_to(self.root / "wrong")
            with self.assertRaises(locks.AccountLockError):
                with locks.account_lock(self.account, kind="codex", inherited_fd=wrong.fileno()):
                    pass
        for fd in (-1, True, 2**100):
            with self.subTest(fd=fd), self.assertRaises(locks.AccountLockError):
                with locks.account_lock(self.account, kind="codex", inherited_fd=fd):
                    pass

    def test_ancestor_symlinks_share_identity_and_final_symlinks_are_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with locks.account_lock(alias / self.account.name, kind="codex"):
            with self.assertRaises(locks.AccountLockError):
                with locks.account_lock(self.account, kind="codex"):
                    pass
        linked_account = self.root / "linked-account"
        linked_account.symlink_to(self.account, target_is_directory=True)
        with self.assertRaisesRegex(locks.AccountLockError, "symlink"):
            with locks.account_lock(linked_account, kind="codex"):
                pass

    def container(self, source=None, state="running", identifier="one"):
        return json.dumps({"id": identifier, "name": "/account-user", "state": state,
                           "mounts": [{"Type": "bind", "Source": str(source or self.account)}]}) + "\n"

    def test_active_containers_using_account_or_a_file_block_operations(self):
        for state in ("created", "running", "paused", "restarting", "removing"):
            for source in (self.account, self.account / "auth.json"):
                with self.subTest(state=state, source=source), patch.object(
                    locks.subprocess, "run", side_effect=[result("one\n"), result(self.container(source, state))],
                ) as run:
                    with self.assertRaisesRegex(locks.AccountLockError, "account-user"):
                        locks.ensure_account_not_in_use(self.account)
                    command = run.call_args.args[0]
                    self.assertEqual(command[:2], ["docker", "inspect"])
                    self.assertNotIn(".Config", " ".join(command))

    def test_stopped_containers_and_unrelated_or_parent_mounts_do_not_block(self):
        for source, state in ((self.account, "exited"), (self.account, "dead"),
                              (self.root, "running"), (self.root / "account with spaces-other", "running")):
            with self.subTest(source=source, state=state), patch.object(
                locks.subprocess, "run", side_effect=[result("one\n"), result(self.container(source, state))],
            ):
                locks.ensure_account_not_in_use(self.account)
        with patch.object(locks.subprocess, "run", return_value=result()) as run:
            locks.ensure_account_not_in_use(self.account)
            self.assertEqual(run.call_count, 1)

    def test_vanished_unrelated_container_does_not_cause_false_busy_failure(self):
        with patch.object(locks.subprocess, "run", side_effect=[
            result("one\ntwo\n"), result(self.container(self.root / "other"), 1), result("one\n"),
        ]) as run:
            locks.ensure_account_not_in_use(self.account)
        self.assertEqual(run.call_count, 3)
        with patch.object(locks.subprocess, "run", side_effect=[result("one\n"), result("", 1), result("")]):
            locks.ensure_account_not_in_use(self.account)

    def test_real_docker_errors_or_still_present_uninspected_containers_fail_closed(self):
        responses = (
            [result("", 1)],
            [result("one\n"), result("", 1), result("one\n")],
            [result("one\n"), result(self.container(self.root / "other"), 1), result("one\n")],
            [result("one\n"), result("", 1), result("", 1)],
            [result("one\n"), result("not JSON\n")],
        )
        for values in responses:
            with self.subTest(values=values), patch.object(locks.subprocess, "run", side_effect=values):
                with self.assertRaises(locks.AccountLockError):
                    locks.ensure_account_not_in_use(self.account)


if __name__ == "__main__":
    unittest.main()
