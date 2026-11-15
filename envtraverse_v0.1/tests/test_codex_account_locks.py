from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.account_locks import account_lock

SCRIPTS = ROOT / "codex_accounts"
DOCKER_STUB = r'''#!/usr/bin/env python3
import fcntl, json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['TEST_DOCKER_LOG'], 'a') as output:
    output.write(json.dumps(args) + '\n')
if args[0] == 'ps':
    if os.environ.get('BUSY_SOURCE'): print('one')
elif args[0] == 'inspect':
    print(json.dumps({'id': 'one', 'name': '/surviving-agent', 'state': 'running',
                      'mounts': [{'Type': 'bind', 'Source': os.environ['BUSY_SOURCE']}]}))
elif args[0] == 'image':
    print('synthetic-image')
elif args[0] in ('run', 'rm'):
    account = Path(os.environ['TEST_ACCOUNT'])
    with (account.parent / ('.' + account.name + '.codex.lock')).open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: pass
        else: raise AssertionError('shell did not retain account lock during Docker run')
    if args[0] == 'rm':
        Path(os.environ['TEST_DOCKER_LOG'] + '.active').unlink(missing_ok=True)
    elif os.environ.get('WAIT_FOR_INTERRUPT') and 'exec' in args:
        Path(os.environ['TEST_DOCKER_LOG'] + '.active').write_text(args[args.index('--name') + 1])
        while True: time.sleep(0.05)
    else:
        print('Logged in using ChatGPT' if args[-2:] == ['login', 'status'] else 'PING')
else:
    raise AssertionError(args)
'''


class CodexAccountLocksTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.account = self.root / "account with spaces"
        self.account.mkdir()
        (self.account / "auth.json").write_text("synthetic auth bytes\n")
        (self.account / "old-history").write_text("old history")
        self.binary = self.root / "bin"
        self.binary.mkdir()
        self.log = self.root / "docker.log"
        self.executable("docker", DOCKER_STUB)
        self.env = {**os.environ, "PATH": f"{self.binary}:{os.environ['PATH']}",
                    "TEST_ACCOUNT": str(self.account), "TEST_DOCKER_LOG": str(self.log)}

    def executable(self, name, contents):
        path = self.binary / name
        path.write_text(contents)
        path.chmod(0o755)

    def run_script(self, script, account=None, *args, pass_fds=(), **env):
        account = self.account if account is None else account
        return subprocess.run(["bash", str(SCRIPTS / script), str(account), *map(str, args)],
                              cwd=self.root, env={**self.env, "TEST_ACCOUNT": str(account), **env},
                              capture_output=True, text=True, timeout=10, pass_fds=pass_fds)

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_standalone_prepare_check_and_interactive_run_hold_locks_during_docker(self):
        for script, account in (("prepare_account.sh", self.root / "new account"),
                                ("check_account.sh", self.account), ("check_account_run.sh", self.account)):
            with self.subTest(script=script):
                outcome = self.run_script(script, account)
                self.assertEqual(outcome.returncode, 0, outcome.stderr)
                self.assertTrue((account / "config.toml").exists() if script == "prepare_account.sh" else True)
        self.assertEqual((self.account / "auth.json").read_text(), "synthetic auth bytes\n")
        self.assertTrue((self.account / "old-history").exists())

    def test_busy_flocks_refuse_all_helpers_before_any_docker_or_changes(self):
        for script in ("prepare_account.sh", "check_account.sh", "clean_account.sh", "check_account_run.sh"):
            account = self.root / "new" if script == "prepare_account.sh" else self.account
            with self.subTest(script=script), account_lock(account, kind="codex"):
                outcome = self.run_script(script, account)
                self.assertNotEqual(outcome.returncode, 0)
                self.assertIn("Account is in use", outcome.stderr)
                self.assertEqual(self.commands(), [])
        self.assertFalse((self.root / "new").exists())
        self.assertTrue((self.account / "old-history").exists())

    def test_surviving_docker_mount_refuses_all_helpers_without_mutation(self):
        for script in ("prepare_account.sh", "check_account.sh", "clean_account.sh", "check_account_run.sh"):
            account = self.root / "new" if script == "prepare_account.sh" else self.account
            with self.subTest(script=script):
                outcome = self.run_script(script, account, BUSY_SOURCE=str(account))
                self.assertNotEqual(outcome.returncode, 0)
                self.assertIn("surviving-agent", outcome.stderr)
        self.assertTrue(all(command[0] in {"ps", "inspect"} for command in self.commands()))
        self.assertFalse((self.root / "new").exists())
        self.assertTrue((self.account / "old-history").exists())

    def test_inherited_check_and_clean_preserve_parent_lock_and_exact_auth_bytes(self):
        with account_lock(self.account, kind="codex") as descriptor:
            for script in ("check_account.sh", "clean_account.sh"):
                outcome = self.run_script(script, self.account, "--lock-fd", descriptor, pass_fds=(descriptor,))
                self.assertEqual(outcome.returncode, 0, outcome.stderr)
                os.fstat(descriptor)
                with (self.root / f".{self.account.name}.codex.lock").open("a") as independent:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(independent, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual((self.account / "auth.json").read_text(), "synthetic auth bytes\n")
        self.assertEqual((self.account / "config.toml").read_text(), 'web_search = "disabled"\n')
        self.assertFalse((self.account / "old-history").exists())

    def test_wrong_inherited_descriptor_cannot_clean_or_check(self):
        with (self.root / "wrong.lock").open("a") as wrong:
            for script in ("check_account.sh", "clean_account.sh"):
                outcome = self.run_script(script, self.account, "--lock-fd", wrong.fileno(), pass_fds=(wrong.fileno(),))
                self.assertNotEqual(outcome.returncode, 0)
                self.assertIn("account lock", outcome.stderr)
        self.assertEqual(self.commands(), [])
        self.assertTrue((self.account / "old-history").exists())
        for script in ("check_account.sh", "clean_account.sh"):
            for descriptor in ("", "-1", "not-a-number"):
                with self.subTest(script=script, descriptor=descriptor):
                    outcome = self.run_script(script, self.account, "--lock-fd", descriptor)
                    self.assertEqual(outcome.returncode, 2, outcome.stderr)
        self.assertEqual(self.commands(), [])

    def test_interrupt_stops_named_ping_container_before_releasing_lock(self):
        active = Path(str(self.log) + ".active")
        with account_lock(self.account, kind="codex") as descriptor:
            with subprocess.Popen(
                ["bash", str(SCRIPTS / "check_account.sh"), str(self.account), "--lock-fd", str(descriptor)],
                cwd=self.root, env={**self.env, "WAIT_FOR_INTERRUPT": "1"}, pass_fds=(descriptor,),
                start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ) as process:
                try:
                    deadline = time.monotonic() + 5
                    while not active.exists():
                        self.assertIsNone(process.poll())
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.01)
                    container_name = active.read_text()
                    os.killpg(process.pid, signal.SIGTERM)
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertEqual(process.returncode, 130, stderr)
                    self.assertNotIn("Account OK", stdout)
                    self.assertFalse(active.exists())
                    self.assertIn(["rm", "--force", container_name], self.commands())
                    # The stub's rm path also verifies that this lock is held.
                    os.fstat(descriptor)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()

    def test_move_holds_source_and_destination_locks_and_preserves_account(self):
        source = self.root / "elsewhere" / "movable"
        source.mkdir(parents=True)
        (source / "auth.json").write_text("synthetic movable auth")
        destination = self.root / "movable"
        # Restrict discovery to our synthetic path instead of scanning the real home.
        self.executable("find", '#!/usr/bin/env bash\nprintf "%s\\n" "$TEST_MOVE_SOURCE"\n')
        self.executable("mv", r'''#!/usr/bin/env python3
import fcntl, shutil, sys
from pathlib import Path
source, destination = map(Path, sys.argv[-2:])
for account in (source, destination):
    with (account.parent / ('.' + account.name + '.codex.lock')).open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: pass
        else: raise AssertionError('move did not retain both account locks')
shutil.move(str(source), str(destination))
''')
        for busy in (source, destination):
            with self.subTest(busy=busy), account_lock(busy, kind="codex"):
                outcome = self.run_script("find_and_move_account.sh", "movable", TEST_MOVE_SOURCE=str(source))
                self.assertNotEqual(outcome.returncode, 0)
                self.assertIn("Account is in use", outcome.stderr)
                self.assertTrue(source.exists())
                self.assertFalse(destination.exists())
        outcome = self.run_script("find_and_move_account.sh", "movable", TEST_MOVE_SOURCE=str(source))
        self.assertEqual(outcome.returncode, 0, outcome.stderr)
        self.assertFalse(source.exists())
        self.assertEqual((destination / "auth.json").read_text(), "synthetic movable auth")


if __name__ == "__main__":
    unittest.main()
