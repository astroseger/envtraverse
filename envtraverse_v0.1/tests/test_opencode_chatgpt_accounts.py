from __future__ import annotations

from contextlib import redirect_stderr
import copy
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "opencode_chatgpt_accounts"
sys.path.insert(0, str(ROOT))
from opencode_chatgpt_accounts import account_tools as MODULE
from src.account_locks import account_lock as controller_account_lock

TOOL_FILES = (
    "Dockerfile", "build_docker.sh", "account_tools.py", "account_locks.py",
    "prepare_account.sh", "check_account.sh", "clean_account.sh",
)
IMAGE = f"opencode-chatgpt-accounts:2.3.0-uid{os.getuid()}-gid{os.getgid()}"
CREDENTIALS = [
    {"id": "cred_chatgpt", "integrationID": "openai", "label": "ChatGPT", "active": True,
     "value": {"type": "oauth", "methodID": "chatgpt-headless", "refresh": "test-refresh-secret",
               "access": "test-access-secret", "expires": 1, "metadata": {"accountID": "test-account"}}},
    {"id": "cred_other", "integrationID": "other", "label": "Other provider", "active": True,
     "value": {"type": "key", "key": "test-other-secret"}},
    {"id": "cred_inactive", "integrationID": "openai", "label": "Inactive key", "active": False,
     "value": {"type": "key", "key": "test-inactive-secret"}},
]

# This stub stores a JSON fixture at opencode.db. Tests exercise the actual shell
# scripts, Python subprocesses, mounts, file replacement, and flock, without a
# Docker daemon, network access, model usage, or real account credentials.
DOCKER_STUB = r'''#!/usr/bin/env python3
import csv
import json
import os
from pathlib import Path
import sys
import time

root = Path(os.environ['TEST_ROOT'])
args = sys.argv[1:]
with (root / 'docker.log').open('a') as log:
    log.write(json.dumps(args) + '\n')
if args[0] == 'ps':
    if os.environ.get('BUSY_MOUNT'): print('orphan')
elif args[0] == 'inspect':
    print(json.dumps({'id': 'orphan', 'name': '/orphan-opencode', 'state': 'running',
                      'mounts': [{'Type': 'bind', 'Source': os.environ['BUSY_MOUNT']}]}))
elif args[0] == 'image':
    if os.environ.get('FAIL_QUERY'):
        sys.exit(23)
    if (root / 'image').exists():
        print('test-image-id')
elif args[0] == 'build':
    (root / 'build-context').write_text(sys.stdin.read())
    print('Build progress on stdout')
    if os.environ.get('FAIL_BUILD'):
        sys.exit(17)
    (root / 'image').touch()
elif args[0] == 'rm':
    pass
elif args[0] == 'run':
    fields = next(csv.reader([args[args.index('--mount') + 1]]))
    mount = dict(field.split('=', 1) for field in fields)
    assert mount['target'] == '/home/user/.local/share/opencode'
    account = Path(mount['source'])
    database = account / 'opencode.db'
    credentials = json.loads((root / 'credentials.json').read_text())
    command = args[args.index('bash') + 2:]
    if 'opencode-account' in command:
        command = command[command.index('opencode-account') + 1:]
    if 'login' in command:
        database.write_text(json.dumps({'credentials': credentials, 'history': []}))
        if os.environ.get('FAIL_LOGIN'):
            sys.exit(18)
        print('Device login completed')
    elif 'export' in command:
        if os.environ.get('DELAY_EXPORT'):
            time.sleep(5)
        if os.environ.get('FAIL_EXPORT'):
            print('test-refresh-secret', file=sys.stderr)
            sys.exit(19)
        if os.environ.get('EMPTY_EXPORT'):
            print('[]')
        elif os.environ.get('INVALID_EXPORT'):
            print('broken JSON test-refresh-secret')
        else:
            print(json.dumps(json.loads(database.read_text())['credentials']))
    elif 'auth import' in command[0]:
        credentials = json.load(sys.stdin)
        if os.environ.get('FAIL_IMPORT'):
            print('test-refresh-secret', file=sys.stderr)
            database.write_text('partial import')
            sys.exit(20)
        if os.environ.get('CHANGED_IMPORT'):
            credentials[0]['value']['access'] = 'changed'
        if os.environ.get('MISSING_IMPORT'):
            credentials = credentials[:1]
        if not os.environ.get('NO_DATABASE'):
            database.write_text(json.dumps({'credentials': credentials, 'history': []}))
        print(json.dumps(list(reversed(credentials))))
    elif 'run' in command:
        data = json.loads(database.read_text())
        data['history'].append('PING check')
        data['credentials'][0]['value']['access'] = 'test-refreshed-access'
        database.write_text(json.dumps(data))
        events = json.loads(os.environ.get('LIVE_EVENTS', '[{"type":"text","part":{"text":"PI"}},'
                          '{"type":"text","part":{"text":"NG"}},'
                          '{"type":"step_finish","part":{"reason":"stop"}}]'))
        for event in events:
            print(json.dumps(event))
        if os.environ.get('FAIL_LIVE'):
            sys.exit(21)
    else:
        raise AssertionError(command)
else:
    raise AssertionError(args)
'''

# Redirect the builder's flock FD in its own shell, keeping test build locks
# independent from a real concurrent image build.
SHELL = '''flock() {
    exec 9>>"$TEST_ROOT/build.lock"
    command flock "$@"
}
export -f flock
exec bash "$@"
'''


class OpenCodeAccountsTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # Explicit source allowlist: never copy any real account directories.
        self.scripts = self.directory / "portable opencode tools"
        self.scripts.mkdir()
        for name in TOOL_FILES:
            shutil.copyfile(SCRIPTS / name, self.scripts / name)
        self.account = self.directory / "account with spaces,comma"
        self.account.mkdir(mode=0o700)
        self.database = self.account / "opencode.db"
        self.data = {"credentials": copy.deepcopy(CREDENTIALS), "history": ["old conversation"]}
        self.database.write_text(json.dumps(self.data))
        (self.account / "log").mkdir()
        (self.account / "log" / "old.log").write_text("old log")
        (self.directory / "credentials.json").write_text(json.dumps(CREDENTIALS))
        (self.directory / "image").touch()
        binary_dir = self.directory / "bin"
        binary_dir.mkdir()
        fake = binary_dir / "docker"
        fake.write_text(DOCKER_STUB)
        fake.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{binary_dir}:{os.environ['PATH']}",
                        TEST_ROOT=str(self.directory), OPENAI_API_KEY="test-host-key-not-forwarded")
        self.env.pop("OPENCODE_MODEL", None)
        self.env.pop("PYTHONPATH", None)
        self.env.pop("PYTHONHOME", None)

    def run_script(self, name, *args, pass_fds=(), **environment):
        result = subprocess.run(
            ["bash", "-c", SHELL, "opencode-accounts-test", str(self.scripts / name), *map(str, args)],
            env=dict(self.env, **environment), cwd=self.directory,
            capture_output=True, text=True, timeout=15, pass_fds=pass_fds,
        )
        for secret in ("test-refresh-secret", "test-access-secret", "test-other-secret", "test-inactive-secret"):
            self.assertNotIn(secret, result.stdout + result.stderr)
        return result

    def commands(self):
        path = self.directory / "docker.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assert_original_preserved(self):
        self.assertEqual(json.loads(self.database.read_text()), self.data)
        self.assertTrue((self.account / "log" / "old.log").exists())
        self.assertFalse(list(self.directory.glob(f".{self.account.name}.clean-*")))

    def test_build_pin_context_output_and_reuse(self):
        result = self.run_script("build_docker.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, IMAGE + "\n")
        self.assertIn("Build progress", result.stderr)
        build = self.commands()[0]
        self.assertEqual(build[-1], "-")
        self.assertIn("OPENCODE_VERSION=2.3.0", build)
        self.assertIn(f"USER_ID={os.getuid()}", build)
        self.assertIn(f"GROUP_ID={os.getgid()}", build)
        self.assertEqual((self.directory / "build-context").read_text(), (SCRIPTS / "Dockerfile").read_text())
        result = self.run_script("build_docker.sh", "--if-missing")
        self.assertEqual(result.stdout, IMAGE + "\n")
        self.assertEqual(sum(command[0] == "build" for command in self.commands()), 1)

    def test_missing_image_is_built_and_build_errors_do_not_create_accounts(self):
        (self.directory / "image").unlink()
        account = self.directory / "new"
        result = self.run_script("prepare_account.sh", account, FAIL_BUILD="1")
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertFalse(account.exists())
        result = self.run_script("prepare_account.sh", account)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(account.stat().st_mode & 0o777, 0o700)
        login = next(command for command in self.commands() if "login" in command)
        self.assertIn("chatgpt-headless", login)
        self.assertIn("--standalone", login)

    def test_docker_query_failure_leaves_account_unchanged(self):
        result = self.run_script("clean_account.sh", self.account, FAIL_QUERY="1")
        self.assertEqual(result.returncode, 23)
        self.assert_original_preserved()
        self.assertEqual([command[0] for command in self.commands()], ["ps", "image"])

    def test_login_can_be_resumed_after_failure(self):
        account = self.directory / "new"
        result = self.run_script("prepare_account.sh", account, FAIL_LOGIN="1")
        self.assertEqual(result.returncode, 18)
        self.assertTrue(account.exists())
        result = self.run_script("prepare_account.sh", account)
        self.assertNotEqual(result.returncode, 0)
        result = self.run_script("prepare_account.sh", account, "--resume")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_live_check_uses_chatgpt_and_persists_refresh_and_history(self):
        result = self.run_script("check_account.sh", self.account, "--model", "openai/test-model")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Account OK", result.stdout)
        commands = [command for command in self.commands() if command[0] == "run"]
        self.assertEqual(len(commands), 2)
        live = commands[1]
        self.assertIn("openai/test-model", live)
        self.assertEqual(live[-1], MODULE.PING_PROMPT)
        self.assertEqual(live[-1].splitlines()[-1], "PING")
        self.assertIn("--standalone", live)
        self.assertIn("--format", live)
        self.assertNotIn("--network", live)
        self.assertNotIn("--env", live)
        self.assertNotIn("test-host-key-not-forwarded", str(commands))
        updated = json.loads(self.database.read_text())
        self.assertEqual(updated["credentials"][0]["value"]["access"], "test-refreshed-access")
        self.assertEqual(updated["history"], ["old conversation", "PING check"])
        self.assertFalse(list(self.directory.glob(f".{self.account.name}.check-*")))

    def test_live_check_rejects_errors_prompt_echoes_and_nonzero_exit(self):
        cases = (
            {"FAIL_LIVE": "1"},
            {"LIVE_EVENTS": json.dumps([{"type": "error", "error": {"message": "PING"}}])},
            {"LIVE_EVENTS": json.dumps([{"type": "step_start", "prompt": "PING"}])},
            {"LIVE_EVENTS": json.dumps([{"type": "text", "part": {"text": "PING extra"}}])},
            {"LIVE_EVENTS": json.dumps([{"type": "text", "part": {"text": "PING."}}])},
            {"LIVE_EVENTS": "[]"},
        )
        for environment in cases:
            with self.subTest(environment=environment):
                result = self.run_script("check_account.sh", self.account, **environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Account OK", result.stdout)

    def test_api_key_inactive_oauth_and_unknown_method_are_rejected_before_live_run(self):
        for change in ({"active": False}, {"value": {"type": "key", "key": "fake"}},
                       {"value": dict(CREDENTIALS[0]["value"], methodID="unrelated-oauth")}):
            with self.subTest(change=change):
                data = copy.deepcopy(self.data)
                data["credentials"][0].update(change)
                self.database.write_text(json.dumps(data))
                result = self.run_script("check_account.sh", self.account)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("No active OpenAI ChatGPT", result.stderr)
        self.assertFalse(any(MODULE.PING_PROMPT in c for c in self.commands()))

    def test_ping_mismatch_reports_bounded_escaped_response(self):
        def output(text):
            return json.dumps({"type": "text", "part": {"text": text}}).encode()

        with self.assertRaisesRegex(MODULE.AccountError, "received 'PING\\.'"):
            MODULE.validate_ping(output("PING."))
        reply = "PING.\n\x1b[31m" + "x" * 200 + "end-of-long-reply"
        with self.assertRaises(MODULE.AccountError) as raised:
            MODULE.validate_ping(output(reply))
        message = str(raised.exception)
        self.assertIn("\\n\\x1b[31m", message)
        self.assertNotIn("\n", message)
        self.assertNotIn("\x1b", message)
        self.assertNotIn("end-of-long-reply", message)
        self.assertIn("truncated", message)
        self.assertLess(len(message), 250)
        with self.assertRaisesRegex(MODULE.AccountError, "model error"):
            MODULE.validate_ping(output("PING") + b'\n{"type":"error"}')

    def test_clean_preserves_all_credentials_and_removes_history_with_two_offline_runs(self):
        outside = self.directory / "do-not-delete"
        outside.write_text("keep")
        (self.account / "symlink-to-outside").symlink_to(outside)
        result = self.run_script("clean_account.sh", self.account)
        self.assertEqual(result.returncode, 0, result.stderr)
        cleaned = json.loads(self.database.read_text())
        self.assertEqual(cleaned, {"credentials": CREDENTIALS, "history": []})
        self.assertEqual(outside.read_text(), "keep")
        self.assertEqual(list(self.account.iterdir()), [self.database])
        self.assertEqual(self.account.stat().st_mode & 0o777, 0o700)
        runs = [command for command in self.commands() if command[0] == "run"]
        self.assertEqual(len(runs), 2)
        self.assertTrue(all(command[command.index("--network") + 1] == "none" for command in runs))
        self.assertFalse(list(self.directory.glob(f".{self.account.name}.clean-*")))

    def test_clean_failures_keep_original_and_remove_temporary_credentials(self):
        for failure in ("FAIL_EXPORT", "EMPTY_EXPORT", "INVALID_EXPORT", "FAIL_IMPORT",
                        "CHANGED_IMPORT", "MISSING_IMPORT", "NO_DATABASE"):
            with self.subTest(failure=failure):
                result = self.run_script("clean_account.sh", self.account, **{failure: "1"})
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assert_original_preserved()

    def test_timeout_stops_container_and_preserves_original(self):
        result = self.run_script("clean_account.sh", self.account, "--timeout", "0.2", DELAY_EXPORT="1")
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertIn("timed out", result.stderr)
        self.assertTrue(any(command[:2] == ["rm", "--force"] for command in self.commands()))
        self.assert_original_preserved()

    def test_all_operations_obey_the_same_external_lock(self):
        lock_path = self.account.parent / f".{self.account.name}.opencode.lock"
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for script, extra in (("prepare_account.sh", ["--resume"]), ("check_account.sh", []),
                                  ("clean_account.sh", [])):
                result = self.run_script(script, self.account, *extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Account is in use", result.stderr)
                self.assertEqual(self.commands(), [])
        result = self.run_script("clean_account.sh", self.account)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(lock_path.exists())

    def test_pending_game_usage_blocks_account_changes_even_if_marker_is_invalid(self):
        marker = MODULE.pending_usage_path(self.account)
        marker.write_text("interrupted marker write")
        for script, extra in (("prepare_account.sh", ["--resume"]), ("check_account.sh", []),
                              ("clean_account.sh", [])):
            with self.subTest(script=script):
                result = self.run_script(script, self.account, *extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("uncollected game usage", result.stderr)
                self.assertIn("controller", result.stderr)
                self.assertNotIn(str(self.scripts.parent / "src"), result.stderr)
                self.assertEqual(self.commands(), [])
                self.assert_original_preserved()
        marker.unlink()
        result = self.run_script("clean_account.sh", self.account)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_orphan_docker_mount_blocks_helpers_after_controller_lock_is_gone(self):
        for script, extra in (("prepare_account.sh", ["--resume"]), ("check_account.sh", []),
                              ("clean_account.sh", [])):
            with self.subTest(script=script):
                result = self.run_script(script, self.account, *extra, BUSY_MOUNT=str(self.account))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("orphan-opencode", result.stderr)
                self.assertTrue(all(command[0] in {"ps", "inspect"} for command in self.commands()))
                self.assert_original_preserved()

    def test_controller_lock_survives_clean_subprocess_using_its_image(self):
        image = "opencode-agent:test-controller-image"
        lock_path = self.account.parent / f".{self.account.name}.opencode.lock"
        with controller_account_lock(self.account, kind="opencode") as descriptor:
            result = self.run_script(
                "clean_account.sh", self.account, "--lock-fd", descriptor, "--image", image,
                pass_fds=(descriptor,), FAIL_QUERY="1", FAIL_BUILD="1",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(self.database.read_text()), {"credentials": CREDENTIALS, "history": []})
            # The child's context and exit must preserve the parent's reservation.
            os.fstat(descriptor)
            with lock_path.open("a") as independent:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(independent, fcntl.LOCK_EX | fcntl.LOCK_NB)
            commands = self.commands()
            self.assertEqual([command[0] for command in commands], ["ps", "run", "run"])
            for command in commands[1:]:
                self.assertEqual(command[command.index("bash") - 1], image)
                self.assertEqual(command[command.index("--network") + 1], "none")
        # The original owner still determines when the reservation ends.
        with MODULE.account_lock(self.account):
            pass

    def test_isolated_clean_rejects_wrong_inherited_descriptor_without_mutation(self):
        with (self.directory / "wrong.lock").open("a") as wrong:
            result = self.run_script(
                "clean_account.sh", self.account, "--lock-fd", wrong.fileno(),
                "--image", "unused", pass_fds=(wrong.fileno(),),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("account lock", result.stderr)
            os.fstat(wrong.fileno())
        self.assertEqual(self.commands(), [])
        self.assert_original_preserved()

    def test_borrowed_lock_is_acquired_without_being_closed_or_unlocked(self):
        lock_path = self.account.parent / f".{self.account.name}.opencode.lock"
        with lock_path.open("a") as inherited:
            with MODULE.account_lock(self.account, inherited.fileno()) as descriptor:
                self.assertEqual(descriptor, inherited.fileno())
            os.fstat(inherited.fileno())
            with lock_path.open("a") as independent:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(independent, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_same_inode_opened_independently_cannot_bypass_parent_lock(self):
        lock_path = self.account.parent / f".{self.account.name}.opencode.lock"
        with MODULE.account_lock(self.account):
            with lock_path.open("a") as independent:
                with self.assertRaisesRegex(MODULE.AccountError, "Account is in use"):
                    with MODULE.account_lock(self.account, independent.fileno()):
                        self.fail("independent descriptor bypassed the lock")
                os.fstat(independent.fileno())

    def test_inherited_lock_rejects_wrong_file_symlink_and_replaced_inode(self):
        lock_path = self.account.parent / f".{self.account.name}.opencode.lock"
        wrong_path = self.directory / "wrong.lock"
        with wrong_path.open("a") as wrong:
            lock_path.touch()
            with self.assertRaises(MODULE.AccountError):
                with MODULE.account_lock(self.account, wrong.fileno()):
                    self.fail("wrong descriptor accepted")
            os.fstat(wrong.fileno())
            lock_path.unlink()
            lock_path.symlink_to(wrong_path)
            with self.assertRaises(MODULE.AccountError):
                with MODULE.account_lock(self.account, wrong.fileno()):
                    self.fail("symlink lock accepted")
            lock_path.unlink()
        with lock_path.open("a") as stale:
            lock_path.rename(self.directory / "replaced.lock")
            lock_path.touch()
            with self.assertRaises(MODULE.AccountError):
                with MODULE.account_lock(self.account, stale.fileno()):
                    self.fail("replaced lock accepted")
            os.fstat(stale.fileno())

    def test_inherited_lock_rejects_unusable_descriptors_before_docker(self):
        lock_path = self.account.parent / f".{self.account.name}.opencode.lock"
        with lock_path.open("a") as closed:
            closed_descriptor = closed.fileno()
        for descriptor in (-1, closed_descriptor, 2 ** 100):
            with self.subTest(descriptor=descriptor), self.assertRaises(MODULE.AccountError):
                with MODULE.account_lock(self.account, descriptor):
                    self.fail("unusable descriptor accepted")
        if hasattr(os, "O_PATH"):
            # This descriptor passes fstat/inode validation but cannot take flock.
            descriptor = os.open(lock_path, os.O_PATH)
            try:
                with self.assertRaisesRegex(MODULE.AccountError, "unusable"):
                    with MODULE.account_lock(self.account, descriptor):
                        self.fail("path-only descriptor accepted")
                os.fstat(descriptor)
            finally:
                os.close(descriptor)
        result = self.run_script("clean_account.sh", self.account, "--lock-fd", "-1", "--image", "unused")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.commands(), [])
        self.assert_original_preserved()

    def test_invalid_targets_and_arguments_fail_before_docker(self):
        symlink = self.directory / "linked"
        symlink.symlink_to(self.account)
        for script, arguments in (
            ("build_docker.sh", ["--unknown"]), ("prepare_account.sh", []),
            ("clean_account.sh", [symlink]), ("clean_account.sh", ["/"]),
            ("clean_account.sh", [self.directory / "missing"]),
            ("clean_account.sh", [self.account, "--timeout", "nan"]),
            ("clean_account.sh", [self.account, "--image", "invalid image"]),
            ("check_account.sh", [self.account, "--lock-fd", "3"]),
            ("prepare_account.sh", [self.account, "--image", "unused"]),
            ("check_account.sh", [self.account, "--model", "other/test"]),
        ):
            with self.subTest(script=script, arguments=arguments):
                result = self.run_script(script, *arguments)
                self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.commands(), [])

    def test_replacement_failure_rolls_back_and_failed_rollback_keeps_backup(self):
        class LocalDocker:
            def run(inner, account, command, **options):
                options["output"].write(json.dumps(CREDENTIALS).encode())
                if options.get("shell"):
                    (account / "opencode.db").write_text("fresh database")

        rename = Path.rename
        for fail_rollback in (False, True):
            def failing_rename(path, target):
                if path.name == "fresh" or (fail_rollback and path.name == "original"):
                    raise OSError("simulated rename failure")
                return rename(path, target)

            with self.subTest(fail_rollback=fail_rollback), patch.object(Path, "rename", failing_rename):
                output = io.StringIO()
                with redirect_stderr(output), self.assertRaises(OSError):
                    MODULE.clean(LocalDocker(), self.account)
            if fail_rollback:
                transaction, = self.directory.glob(f".{self.account.name}.clean-*")
                backup = transaction / "original"
                self.assertEqual(json.loads((backup / "opencode.db").read_text()), self.data)
                self.assertIn(str(backup), output.getvalue())
                backup.rename(self.account)
                shutil.rmtree(transaction)
            self.assert_original_preserved()


@unittest.skipUnless(os.environ.get("OPENCODE_ACCOUNTS_DOCKER_TESTS") == "1",
                     "Set OPENCODE_ACCOUNTS_DOCKER_TESTS=1 to test the real image with synthetic credentials")
class OpenCodeDockerIntegrationTest(unittest.TestCase):
    def test_real_credential_round_trip_removes_database_history(self):
        with tempfile.TemporaryDirectory(prefix="opencode-accounts-integration-") as temporary:
            directory = Path(temporary)
            account = directory / "account"
            account.mkdir(mode=0o700)
            source = directory / "synthetic.json"
            source.write_text(json.dumps(CREDENTIALS))
            source.chmod(0o600)
            docker = MODULE.Docker(MODULE.build_image(), 30)
            with source.open("rb") as credentials:
                docker.run(account, ["opencode", "auth", "import", "--standalone"],
                           operation="Synthetic credential import", input_file=credentials, offline=True)
            original = MODULE.export_credentials(docker, account, directory / "before.json")
            # An invalid model creates a session/user message but cannot make a
            # model request. Networking is also disabled for every container.
            with self.assertRaises(MODULE.AccountError):
                docker.run(account, ["opencode", "run", "--standalone", "--auto", "--format", "json",
                                     "--model", "openai/does-not-exist-smoke-test", "Synthetic offline conversation"],
                           operation="Offline conversation", offline=True)
            with sqlite3.connect(account / "opencode.db") as database:
                self.assertGreater(database.execute("SELECT count(*) FROM session_v2").fetchone()[0], 0)
                self.assertGreater(database.execute("SELECT count(*) FROM session_message").fetchone()[0], 0)
            with MODULE.account_lock(account) as descriptor:
                result = subprocess.run(
                    ["bash", str(SCRIPTS / "clean_account.sh"), str(account),
                     "--lock-fd", str(descriptor), "--image", docker.image],
                    capture_output=True, text=True, timeout=90, pass_fds=(descriptor,),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                os.fstat(descriptor)
                lock_path = account.parent / f".{account.name}.opencode.lock"
                with lock_path.open("a") as independent:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(independent, fcntl.LOCK_EX | fcntl.LOCK_NB)
            restored = MODULE.export_credentials(docker, account, directory / "after.json")
            self.assertEqual(restored, original)
            self.assertEqual((account / "opencode.db").stat().st_mode & 0o777, 0o600)
            self.assertEqual(account.stat().st_mode & 0o777, 0o700)
            with sqlite3.connect(account / "opencode.db") as database:
                self.assertEqual(database.execute("SELECT count(*) FROM session_v2").fetchone()[0], 0)
                self.assertEqual(database.execute("SELECT count(*) FROM session_message").fetchone()[0], 0)
            self.assertFalse(list(directory.glob(".account.clean-*")))


if __name__ == "__main__":
    unittest.main()
