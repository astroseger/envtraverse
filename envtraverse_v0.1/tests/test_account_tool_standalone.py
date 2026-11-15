from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StandaloneAccountToolsTest(unittest.TestCase):
    def test_vendored_lock_helpers_match_the_controller_contract(self):
        canonical = (ROOT / "src/account_locks.py").read_bytes()
        for folder in ("codex_accounts", "opencode_chatgpt_accounts"):
            with self.subTest(folder=folder):
                self.assertEqual(
                    (ROOT / folder / "account_locks.py").read_bytes(), canonical,
                    "Update both standalone copies when changing the shared account-lock contract.",
                )

    def test_renamed_packages_import_without_checkout_or_module_name_collisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            packages = directory / "portable tools"
            packages.mkdir()
            # Only importable source files are needed here. The account helper
            # suites separately exercise copied shell entry points with Docker stubs.
            for source, target, files in (
                ("codex_accounts", "portable_codex", ("account_locks.py", "check_account.py")),
                ("opencode_chatgpt_accounts", "portable_opencode", ("account_locks.py", "account_tools.py")),
            ):
                destination = packages / target
                destination.mkdir()
                for name in files:
                    shutil.copyfile(ROOT / source / name, destination / name)
            working_directory = directory / "unrelated cwd"
            working_directory.mkdir()
            environment = {key: value for key, value in os.environ.items()
                           if key not in {"PYTHONPATH", "PYTHONHOME"}}
            result = subprocess.run(
                [sys.executable, "-S", "-c", """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from portable_codex import account_locks as codex_locks, check_account
from portable_opencode import account_locks as opencode_locks, account_tools
assert check_account.AccountLockError is codex_locks.AccountLockError
assert not any(name == 'src' or name.startswith('src.') for name in sys.modules)
assert 'account_locks' not in sys.modules
account = Path.cwd() / 'synthetic-account'
account.mkdir()
with opencode_locks.account_lock(account, kind='opencode'):
    try:
        with account_tools.account_lock(account):
            raise AssertionError('the account was acquired twice')
    except account_tools.AccountError as error:
        assert 'Account is in use' in str(error)
""", str(packages)],
                cwd=working_directory, env=environment, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
