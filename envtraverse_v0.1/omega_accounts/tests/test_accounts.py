"""Offline account-script tests: fake Docker/Git, temporary account data only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
REVISION = "1234567890abcdef1234567890abcdef12345678"
IMAGE = f"omega-accounts-image-unified-agent-v1-0:{os.getuid()}-{os.getgid()}"

FAKE_TOOL = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import time

name, args = Path(sys.argv[0]).name, sys.argv[1:]
with open(os.environ["OMEGA_TEST_LOG"], "a") as target:
    target.write(json.dumps([name, *args]) + "\n")

def fail(code, text):
    print(text, file=sys.stderr)
    raise SystemExit(code)

if name == "docker":
    if args[:2] == ["image", "ls"]:
        code = int(os.environ.get("OMEGA_TEST_QUERY_STATUS", "0"))
        if code:
            fail(code, "Docker daemon unavailable")
        if (os.environ.get("OMEGA_TEST_IMAGE_PRESENT") == "1"
                or Path(os.environ["OMEGA_TEST_IMAGE_STATE"]).exists()):
            print("fake-image-id")
    elif args and args[0] == "build":
        print("Docker build stdout")
        print("Docker build stderr", file=sys.stderr)
        if args[-1] == "-":
            sys.stdin.read()
        release = os.environ.get("OMEGA_TEST_RELEASE")
        if release:
            Path(os.environ["OMEGA_TEST_BUILD_STARTED"]).touch()
            while not Path(release).exists():
                time.sleep(0.01)
        code = int(os.environ.get("OMEGA_TEST_BUILD_STATUS", "0"))
        if code:
            fail(code, "Image build failed")
        tag = args[args.index("-t") + 1]
        if tag.startswith("omega-accounts-"):
            Path(os.environ["OMEGA_TEST_IMAGE_STATE"]).touch()
    elif args and args[0] == "run":
        if "login" in args:
            code = int(os.environ.get("OMEGA_TEST_LOGIN_STATUS", "0"))
            if code:
                fail(code, "Sign-in failed; registration retained")
            print("Signed in (offline fake)")
        elif "check" in args:
            code = int(os.environ.get("OMEGA_TEST_CHECK_STATUS", "0"))
            if code:
                fail(code, "Account check failed (offline fake)")
            print("Account OK: /home/user/.omega_auth/chatgpt.json (model: fake-model)")
        else:
            fail(99, "Unexpected docker run: " + repr(args))
    else:
        fail(99, "Unexpected Docker command: " + repr(args))
elif name == "git":
    if args and args[0] == "clone":
        code = int(os.environ.get("OMEGA_TEST_CLONE_STATUS", "0"))
        if code:
            fail(code, "Clone failed")
        destination = Path(args[-1])
        destination.mkdir(parents=True)
        (destination / "Dockerfile").write_text("FROM scratch\n")
        (destination / "scripts").mkdir()
        (destination / "scripts" / "chatgpt-auth").write_text("# fake auth helper\n")
        (destination / "src").mkdir()
        (destination / "src" / "check_account.py").write_text("# fake check helper\n")
        print("Git clone progress", file=sys.stderr)
    elif "rev-parse" in args:
        print(os.environ["OMEGA_TEST_REVISION"])
    else:
        fail(99, "Unexpected Git command: " + repr(args))
else:
    fail(99, "Unexpected executable")
'''


class AccountScriptsTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="omega-script-test-")))
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        for tool in ("docker", "git"):
            path = self.bin / tool
            path.write_text(FAKE_TOOL)
            path.chmod(0o755)
        self.log = self.directory / "commands.jsonl"
        self.image_state = self.directory / "image-exists"
        self.account = self.directory / "account with spaces:local"
        self.account.mkdir(mode=0o700)
        (self.account / "chatgpt.json").write_text('{"test_registration": true}\n')
        (self.account / "chatgpt.json").chmod(0o600)
        # Replace the inherited lock descriptor inside the shell, retaining a
        # real flock while preventing tests from locking a production build.
        self.shell_env = self.directory / "bash-env"
        self.shell_env.write_text('flock() { printf "%s\\n" lock >>"$OMEGA_TEST_LOCK_EVENTS"; exec 9>>"$OMEGA_TEST_LOCK"; command flock "$@"; }\nexport -f flock\n')

    def invocation(self, script, *arguments, **settings):
        environment = dict(os.environ)
        environment.update(
            PATH=str(self.bin) + os.pathsep + environment["PATH"],
            BASH_ENV=str(self.shell_env), OMEGA_TEST_LOG=str(self.log),
            OMEGA_TEST_IMAGE_PRESENT="1", OMEGA_TEST_IMAGE_STATE=str(self.image_state),
            OMEGA_TEST_REVISION=REVISION, OMEGA_TEST_LOCK=str(self.directory / "build.lock"),
            OMEGA_TEST_LOCK_EVENTS=str(self.directory / "lock-events"),
            OMEGA_TEST_BUILD_STARTED=str(self.directory / "build-started"),
        )
        environment.update({f"OMEGA_TEST_{key.upper()}": str(value) for key, value in settings.items()})
        return dict(args=["bash", str(SCRIPTS / script), *map(str, arguments)],
                    cwd=self.directory, env=environment, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def run_script(self, script, *arguments, **settings):
        self.log.unlink(missing_ok=True)
        self.image_state.unlink(missing_ok=True)
        result = subprocess.run(**self.invocation(script, *arguments, **settings), timeout=10)
        commands = self.commands()
        return result, commands

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    @staticmethod
    def docker_runs(commands):
        return [command for command in commands if command[:2] == ["docker", "run"]]

    def test_prepare_mounts_private_directory_and_uses_browser_login(self):
        account = self.directory / "new account:local"
        result, commands = self.run_script("prepare_account.sh", account)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(account.stat().st_mode & 0o777, 0o700)
        run, = self.docker_runs(commands)
        mount = run[run.index("--mount") + 1]
        self.assertIn(str(account), mount)
        self.assertIn("/home/user/.omega_auth", mount)
        self.assertNotIn("readonly", mount)
        self.assertEqual(run[run.index("--network") + 1], "host")
        self.assertIn("--rm", run)
        self.assertIn("--pull=never", run)
        self.assertEqual(run[-3:], ["/PeTTa/repos/Omega/scripts/chatgpt-auth", "login", "--no-browser"])
        self.assertFalse((account / "config.toml").exists())

    def test_prepare_refuses_existing_directory_before_docker(self):
        result, commands = self.run_script("prepare_account.sh", self.account)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(commands, [])
        self.assertTrue((self.account / "chatgpt.json").exists())

    def test_resume_retains_registration_even_when_login_fails(self):
        before = (self.account / "chatgpt.json").read_bytes()
        result, commands = self.run_script("prepare_account.sh", "--resume", self.account, login_status=7)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual((self.account / "chatgpt.json").read_bytes(), before)
        self.assertEqual(len(self.docker_runs(commands)), 1)

    def test_check_forwards_model_timeout_and_mounts_whole_directory(self):
        result, commands = self.run_script("check_account.sh", self.account,
                                          "--model", "account-model", "--timeout", "9")
        self.assertEqual(result.returncode, 0, result.stderr)
        run, = self.docker_runs(commands)
        mount = run[run.index("--mount") + 1]
        self.assertIn(str(self.account), mount)
        self.assertIn("/home/user/.omega_auth", mount)
        self.assertNotIn("readonly", mount)
        self.assertEqual(run[run.index("--model") + 1], "account-model")
        self.assertEqual(run[run.index("--timeout") + 1], "9")
        self.assertIn("check", run)
        self.assertIn("Account OK", result.stdout)
        self.assertIn(str(self.account), result.stdout)
        self.assertNotIn("login", run)

    def test_check_preserves_failure_timeout_and_cancellation_codes(self):
        for code in (1, 124, 130):
            with self.subTest(code=code):
                result, commands = self.run_script("check_account.sh", self.account, check_status=code)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertNotIn("Account OK", result.stdout)
                self.assertEqual(len(self.docker_runs(commands)), 1)

    def test_missing_or_symlinked_credentials_fail_before_docker(self):
        credential = self.account / "chatgpt.json"
        credential.unlink()
        result, commands = self.run_script("check_account.sh", self.account)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(commands, [])
        target = self.directory / "dummy.json"
        target.write_text("{}")
        credential.symlink_to(target)
        result, commands = self.run_script("check_account.sh", self.account)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(commands, [])

    def test_invalid_arguments_fail_before_docker(self):
        cases = (
            ("prepare_account.sh",),
            ("prepare_account.sh", "--unknown"),
            ("check_account.sh",),
            ("check_account.sh", self.account, "--timeout", "0"),
            ("check_account.sh", self.account, "--timeout", "-1"),
            ("check_account.sh", self.account, "--timeout", "abc"),
            ("check_account.sh", self.account, "--model"),
            ("check_account.sh", self.account, "--unknown"),
            ("build_docker.sh", "--unknown"),
            ("build_docker.sh", "--if-missing", "extra"),
        )
        for script, *arguments in cases:
            with self.subTest(script=script, arguments=arguments):
                result, commands = self.run_script(script, *arguments)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(commands, [])

    def test_unsupported_mount_path_fails_before_docker(self):
        for component in ("comma,path", 'quote"path', "newline\npath"):
            with self.subTest(component=component):
                result, commands = self.run_script("prepare_account.sh", self.directory / component)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(commands, [])

    def test_docker_query_error_propagates_without_creating_account(self):
        account = self.directory / "new-account"
        result, commands = self.run_script("prepare_account.sh", account, query_status=23)
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertFalse(account.exists())
        self.assertFalse(self.docker_runs(commands))

    def test_existing_image_is_reused_without_git_or_build(self):
        result, commands = self.run_script("build_docker.sh", "--if-missing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, IMAGE + "\n")
        self.assertEqual(commands, [["docker", "image", "ls", "--quiet", "--filter", f"reference={IMAGE}"]])

    def test_manual_build_uses_clean_clone_and_empty_wrapper_context(self):
        result, commands = self.run_script("build_docker.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, IMAGE + "\n")
        self.assertIn("Docker build stdout", result.stderr)
        self.assertIn("Docker build stderr", result.stderr)
        clones = [command for command in commands if command[:2] == ["git", "clone"]]
        clone, = clones
        self.assertIn("https://github.com/astroseger/Omega.git", clone)
        self.assertEqual(clone[clone.index("--branch") + 1], "chatgpt-subscription")
        source = Path(clone[-1])
        self.assertNotEqual(source, self.account)
        builds = [command for command in commands if command[:2] == ["docker", "build"]]
        base, wrapper = builds
        self.assertEqual(base[-1], str(source))
        self.assertIn(f"omega-subscription-base:{REVISION}", base)
        self.assertEqual(wrapper[-1], "-")
        self.assertIn(f"OMEGA_BASE_IMAGE=omega-subscription-base:{REVISION}", wrapper)
        self.assertIn(f"OMEGA_REVISION={REVISION}", wrapper)
        self.assertIn(f"USER_ID={os.getuid()}", wrapper)
        self.assertIn(f"GROUP_ID={os.getgid()}", wrapper)
        self.assertIn(IMAGE, wrapper)
        self.assertFalse(source.exists(), "temporary clone must be cleaned after building")

    def test_missing_image_is_double_checked_before_build(self):
        result, commands = self.run_script("build_docker.sh", "--if-missing", image_present=0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, IMAGE + "\n")
        queries = [command for command in commands if command[:3] == ["docker", "image", "ls"]]
        self.assertEqual(len(queries), 2)
        self.assertTrue(self.image_state.exists())

    def test_build_failure_stops_before_login_or_account_creation(self):
        account = self.directory / "new-account"
        result, commands = self.run_script("prepare_account.sh", account, image_present=0, build_status=17)
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertNotIn(IMAGE, result.stdout)
        self.assertFalse(account.exists())
        self.assertFalse(self.docker_runs(commands))
        clone, = [command for command in commands if command[:2] == ["git", "clone"]]
        self.assertFalse(Path(clone[-1]).exists())

    def test_clone_failure_never_reports_an_image(self):
        result, commands = self.run_script("build_docker.sh", image_present=0, clone_status=19)
        self.assertEqual(result.returncode, 19, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(any(command[:2] == ["docker", "build"] for command in commands))


    def test_concurrent_first_uses_build_once_and_recheck_after_lock(self):
        release = self.directory / "release-build"
        started = self.directory / "build-started"
        lock_events = self.directory / "lock-events"

        def wait_until(condition, process):
            deadline = time.monotonic() + 5
            while not condition():
                self.assertIsNone(process.poll(), "worker exited before synchronization")
                self.assertLess(time.monotonic(), deadline, "synchronization timed out")
                time.sleep(0.01)

        with subprocess.Popen(**self.invocation(
            "build_docker.sh", "--if-missing", image_present=0, release=release,
        )) as first:
            try:
                wait_until(started.exists, first)
                with subprocess.Popen(**self.invocation(
                    "build_docker.sh", "--if-missing", image_present=0,
                )) as second:
                    try:
                        wait_until(lambda: len(lock_events.read_text().splitlines()) >= 2, second)
                        clones = [cmd for cmd in self.commands() if cmd[:2] == ["git", "clone"]]
                        self.assertEqual(len(clones), 1, "second build must wait for first")
                    finally:
                        release.touch()
                    stdout, stderr = second.communicate(timeout=10)
                    self.assertEqual(second.returncode, 0, stderr)
                    self.assertEqual(stdout, IMAGE + "\n")
            finally:
                release.touch()
            stdout, stderr = first.communicate(timeout=10)
            self.assertEqual(first.returncode, 0, stderr)
            self.assertEqual(stdout, IMAGE + "\n")
        builds = [cmd for cmd in self.commands() if cmd[:2] == ["docker", "build"]]
        self.assertEqual(len(builds), 2, "one base build and one wrapper build")


if __name__ == "__main__":
    unittest.main()
