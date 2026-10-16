from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT_SCRIPTS = ROOT / "codex_accounts"
IMAGE = "codex-accounts-image-unified-agent-v1-0"
IMAGE_QUERY = ["image", "ls", "--quiet", "--filter", f"reference={IMAGE}:latest"]

# No Docker daemon or real account is used. The real flock command is exercised
# on a private test lock so tests cannot block a real account-image build.
DOCKER_STUB = r'''
docker() {
    printf '%s\0' "$@" '' >> "$DOCKER_TEST_LOG"
    if [[ "$1" == image ]]; then
        if [[ "$DOCKER_TEST_QUERY_STATUS" != 0 ]]; then
            printf 'Docker daemon unavailable\n' >&2
            return "$DOCKER_TEST_QUERY_STATUS"
        fi
        if [[ -f "$DOCKER_TEST_IMAGE" ]]; then
            printf 'test-image-id\n'
        fi
    elif [[ "$1" == build ]]; then
        printf 'Docker build progress on stdout\n'
        printf 'Docker build progress on stderr\n' >&2
        # Consume the Dockerfile from stdin, not a credential-containing context.
        while IFS= read -r line; do :; done
        : > "$DOCKER_TEST_LOG.build-started"
        while [[ -n "$DOCKER_TEST_RELEASE" && ! -f "$DOCKER_TEST_RELEASE" ]]; do
            sleep 0.01
        done
        if [[ "$DOCKER_TEST_BUILD_STATUS" != 0 ]]; then
            printf 'Image build failed\n' >&2
            return "$DOCKER_TEST_BUILD_STATUS"
        fi
        : > "$DOCKER_TEST_IMAGE"
    elif [[ " $* " == *" login status "* ]]; then
        printf 'Logged in using ChatGPT\n'
    else
        printf 'PING\n'
    fi
}
flock() {
    printf '%s\n' "$@" >> "$DOCKER_TEST_LOG.lock-attempt"
    exec 9>>"$DOCKER_TEST_LOCK"
    command flock "$@"
}
export -f docker flock
'''


class AccountImageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.account = self.directory / "account"
        self.account.mkdir()
        self.log = self.directory / "docker-commands"
        self.image_state = self.directory / "image-exists"

    def invocation(self, name, *args, scripts_dir=ACCOUNT_SCRIPTS, log=None,
                   query_status=0, build_status=0, release=""):
        command = DOCKER_STUB + '\nexec bash "$@"\n'
        return dict(
            args=["bash", "-c", command, "account-image-test", str(scripts_dir / name), *map(str, args)],
            cwd=self.directory, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=dict(
                os.environ, DOCKER_TEST_LOG=str(log or self.log),
                DOCKER_TEST_IMAGE=str(self.image_state),
                DOCKER_TEST_LOCK=str(self.directory / "build.lock"),
                DOCKER_TEST_QUERY_STATUS=str(query_status),
                DOCKER_TEST_BUILD_STATUS=str(build_status),
                DOCKER_TEST_RELEASE=str(release), OPENAI_API_KEY="test-not-a-real-key",
            ),
        )

    @staticmethod
    def commands(log):
        return [entry.split("\0") for entry in log.read_text().split("\0\0") if entry] if log.exists() else []

    def run_script(self, name, *args, image_present=True, **options):
        self.log.write_text("")
        Path(f"{self.log}.lock-attempt").unlink(missing_ok=True)
        if image_present:
            self.image_state.touch()
        else:
            self.image_state.unlink(missing_ok=True)
        result = subprocess.run(
            **self.invocation(name, *args, **options), timeout=10,
        )
        return result, self.commands(self.log)

    def account_commands(self):
        return (
            ("prepare_account.sh", self.directory / "new-account"),
            ("check_account.sh", self.account),
            ("check_account_run.sh", self.account),
            ("check_codex_with_api_key.sh",),
        )

    def test_manual_build_has_only_a_dockerfile_as_context_and_uses_the_fixed_tag(self) -> None:
        result, commands = self.run_script("build_docker.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{IMAGE}\n")
        self.assertIn("Docker build progress on stdout", result.stderr)
        self.assertIn("Docker build progress on stderr", result.stderr)
        self.assertEqual(len(commands), 1)
        build = commands[0]
        self.assertEqual(build[0], "build")
        self.assertEqual(build[-1], "-")
        self.assertEqual(build[build.index("-t") + 1], IMAGE)
        self.assertIn(f"USER_ID={os.getuid()}", build)
        self.assertIn(f"GROUP_ID={os.getgid()}", build)
        self.assertEqual(Path(f"{self.log}.lock-attempt").read_text(), "9\n")
        dockerfile = (ACCOUNT_SCRIPTS / "Dockerfile").read_text()
        self.assertIn("@openai/codex", dockerfile)
        self.assertNotIn("COPY", dockerfile)
        self.assertNotIn("numpy", dockerfile)

    def test_all_account_commands_use_the_fixed_image_without_building(self) -> None:
        for script in self.account_commands():
            with self.subTest(script=script[0]):
                result, commands = self.run_script(*script)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(commands[0], IMAGE_QUERY)
                self.assertFalse(Path(f"{self.log}.lock-attempt").exists())
                self.assertTrue(any(command[0] == "run" for command in commands))
                for command in commands[1:]:
                    self.assertEqual(command[0], "run")
                    self.assertIn(IMAGE, command)
                    self.assertIn("--pull=never", command)
                    if script[0] != "check_codex_with_api_key.sh":
                        self.assertIn(f"{script[1]}:/home/user/.codex", command)

    def test_all_account_commands_build_a_missing_image_before_running(self) -> None:
        for script in self.account_commands():
            with self.subTest(script=script[0]):
                result, commands = self.run_script(*script, image_present=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(commands[:2], [IMAGE_QUERY, IMAGE_QUERY])
                self.assertEqual(commands[2][0], "build")
                self.assertTrue(commands[3:])
                for command in commands[3:]:
                    self.assertEqual(command[0], "run")
                    self.assertIn(IMAGE, command)
                    self.assertIn("--pull=never", command)
                self.assertNotIn("Docker build progress", result.stdout)
                self.assertIn("Docker build progress on stdout", result.stderr)
                self.assertIn("Docker build progress on stderr", result.stderr)
                self.assertTrue(self.image_state.exists())
                self.assertIn(IMAGE, result.stderr)

    def test_if_missing_reuses_the_image_built_by_an_earlier_call(self) -> None:
        result, commands = self.run_script("build_docker.sh", "--if-missing", image_present=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{IMAGE}\n")
        self.assertEqual([command[0] for command in commands], ["image", "image", "build"])
        result = subprocess.run(**self.invocation("build_docker.sh", "--if-missing"), timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"{IMAGE}\n")
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.commands(self.log), commands + [IMAGE_QUERY])

    def test_docker_errors_stop_account_commands_without_building(self) -> None:
        for script in self.account_commands():
            with self.subTest(script=script[0]):
                result, commands = self.run_script(*script, query_status=23)
                self.assertEqual(result.returncode, 23, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("Docker daemon unavailable", result.stderr)
                self.assertEqual(commands, [IMAGE_QUERY])
                self.assertFalse(Path(f"{self.log}.lock-attempt").exists())
                self.assertFalse((self.directory / "new-account").exists())

    def test_build_failure_stops_account_commands_without_creating_an_account(self) -> None:
        for script in self.account_commands():
            with self.subTest(script=script[0]):
                result, commands = self.run_script(*script, image_present=False, build_status=17)
                self.assertEqual(result.returncode, 17, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("Image build failed", result.stderr)
                self.assertEqual([command[0] for command in commands], ["image", "image", "build"])
                self.assertFalse(self.image_state.exists())
                self.assertFalse((self.directory / "new-account").exists())

    def test_invalid_arguments_fail_before_contacting_docker(self) -> None:
        for args in (("--unknown",), ("--if-missing", "extra"), ("",)):
            with self.subTest(args=args):
                result, commands = self.run_script("build_docker.sh", *args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("[--if-missing]", result.stderr)
                self.assertEqual(commands, [])

    def test_failed_build_script_prints_no_image_name(self) -> None:
        for args, options, status in (
            (("--if-missing",), {"query_status": 23}, 23),
            (("--if-missing",), {"build_status": 17}, 17),
            ((), {"build_status": 17}, 17),
        ):
            with self.subTest(args=args, options=options):
                result, _ = self.run_script("build_docker.sh", *args, image_present=False, **options)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(result.stdout, "")

    def wait_for_file(self, path, process):
        deadline = time.monotonic() + 5
        while not path.exists():
            self.assertIsNone(process.poll(), f"Process exited before creating {path}")
            self.assertLess(time.monotonic(), deadline, f"Timed out waiting for {path}")
            time.sleep(0.01)

    def test_concurrent_builds_share_a_lock_across_experiment_copies(self) -> None:
        other_scripts = self.directory / "another experiment" / "codex_accounts"
        other_scripts.mkdir(parents=True)
        for name in ("build_docker.sh", "Dockerfile"):
            shutil.copyfile(ACCOUNT_SCRIPTS / name, other_scripts / name)

        for index, (first_args, second_args, expected_builds) in enumerate((
            (("--if-missing",), ("--if-missing",), 1),
            ((), ("--if-missing",), 1),
            (("--if-missing",), (), 2),
        )):
            with self.subTest(first_args=first_args, second_args=second_args):
                self.image_state.unlink(missing_ok=True)
                first_log = self.directory / f"first-{index}"
                second_log = self.directory / f"second-{index}"
                release = self.directory / f"release-{index}"
                with subprocess.Popen(**self.invocation(
                    "build_docker.sh", *first_args, log=first_log, release=release,
                )) as first:
                    try:
                        self.wait_for_file(Path(f"{first_log}.build-started"), first)
                        with subprocess.Popen(**self.invocation(
                            "build_docker.sh", *second_args, log=second_log,
                            scripts_dir=other_scripts,
                        )) as second:
                            try:
                                self.wait_for_file(Path(f"{second_log}.lock-attempt"), second)
                                self.assertFalse(Path(f"{second_log}.build-started").exists())
                            finally:
                                release.touch()
                            stdout, stderr = second.communicate(timeout=10)
                            self.assertEqual(second.returncode, 0, stderr)
                            self.assertEqual(stdout, f"{IMAGE}\n")
                    finally:
                        release.touch()
                    stdout, stderr = first.communicate(timeout=10)
                    self.assertEqual(first.returncode, 0, stderr)
                    self.assertEqual(stdout, f"{IMAGE}\n")
                commands = self.commands(first_log) + self.commands(second_log)
                self.assertEqual(sum(command[0] == "build" for command in commands), expected_builds)
                if second_args:
                    self.assertEqual(self.commands(second_log), [IMAGE_QUERY, IMAGE_QUERY])


if __name__ == "__main__":
    unittest.main()
