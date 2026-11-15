"""Shared advisory account locks and checks for surviving Docker account users."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


class AccountLockError(RuntimeError):
    pass


@contextmanager
def account_lock(account: Path, *, kind: str, inherited_fd: int | None = None):
    """Hold a sibling flock; borrowing a descriptor never unlocks or closes it."""
    if kind not in {"codex", "opencode"}:
        raise AccountLockError(f"Unsupported account lock kind: {kind!r}")
    account = Path(account).expanduser()
    if account.is_symlink():
        raise AccountLockError("The account directory must not be a symlink.")
    account = account.resolve()
    lock_path = account.parent / f".{account.name}.{kind}.lock"
    owned = inherited_fd is None
    fd = None
    try:
        if owned:
            try:
                fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            except OSError as exc:
                raise AccountLockError(f"Cannot open account lock {lock_path}: {exc.strerror}") from None
        else:
            fd = inherited_fd
            if type(fd) is not int or fd < 0:
                raise AccountLockError("Invalid inherited account lock descriptor.")
        try:
            descriptor = os.fstat(fd)
            expected = lock_path.lstat()
        except (OSError, OverflowError, ValueError):
            raise AccountLockError(f"Cannot validate account lock descriptor for {lock_path}.") from None
        if (not stat.S_ISREG(descriptor.st_mode) or not stat.S_ISREG(expected.st_mode)
                or (descriptor.st_dev, descriptor.st_ino) != (expected.st_dev, expected.st_ino)):
            raise AccountLockError(f"Descriptor does not match the regular account lock file: {lock_path}.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AccountLockError(f"Account is in use (lock: {lock_path}).") from None
        except OSError:
            raise AccountLockError(f"Account lock descriptor is unusable: {lock_path}.") from None
        yield fd
    finally:
        if owned and fd is not None:
            os.close(fd)


_INSPECT_FORMAT = '{"id":{{json .Id}},"name":{{json .Name}},"state":{{json .State.Status}},"mounts":{{json .Mounts}}}'
_ACTIVE_STATES = {"created", "running", "paused", "restarting", "removing"}
_INACTIVE_STATES = {"exited", "dead"}


def _docker_read(command: list[str], operation: str, *, allow_failure: bool = False) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AccountLockError(f"Cannot {operation} to check account use: {type(exc).__name__}") from None
    if result.returncode and not allow_failure:
        raise AccountLockError(f"Cannot {operation} to check account use (exit {result.returncode}); retry when Docker is available")
    return result


def ensure_account_not_in_use(account: Path) -> None:
    """Refuse active Docker binds of this account or a file within it.

    Check under the account lock. Parent-directory binds (such as a developer's
    whole checkout) are deliberately excluded. Docker metadata inspection asks
    only for names, states and mounts, never environment or credential contents.
    """
    account = Path(account).resolve()
    list_command = ["docker", "ps", "--all", "--quiet", "--no-trunc"]
    identifiers = _docker_read(list_command, "list Docker containers").stdout.split()
    if not identifiers:
        return
    result = _docker_read(["docker", "inspect", "--format", _INSPECT_FORMAT, *identifiers],
                          "inspect Docker containers", allow_failure=True)
    inspected = set()
    for line in result.stdout.splitlines():
        try:
            container = json.loads(line)
        except (ValueError, UnicodeError):
            raise AccountLockError("Docker inspection returned invalid account-use information") from None
        if not isinstance(container, dict) or not isinstance(container.get("mounts"), list):
            raise AccountLockError("Docker inspection returned invalid account-use information")
        identifier = container.get("id")
        if not isinstance(identifier, str) or identifier not in identifiers or identifier in inspected:
            raise AccountLockError("Docker inspection returned invalid container IDs")
        inspected.add(identifier)
        state = container.get("state")
        if not isinstance(state, str):
            raise AccountLockError("Docker inspection returned an unknown container state")
        if state in _INACTIVE_STATES:
            continue
        if state not in _ACTIVE_STATES:
            raise AccountLockError("Docker inspection returned an unknown container state")
        for mount in container["mounts"]:
            if not isinstance(mount, dict):
                raise AccountLockError("Docker inspection returned an invalid mount")
            if mount.get("Type") != "bind":
                continue
            source = mount.get("Source")
            if not isinstance(source, str) or not source:
                raise AccountLockError("Docker inspection returned an invalid bind source")
            if Path(source).resolve().is_relative_to(account):
                name = container.get("name")
                if not isinstance(name, str) or not name:
                    raise AccountLockError("Docker inspection returned an invalid container name")
                name = name.lstrip("/")
                raise AccountLockError(
                    f"Account is in use by Docker container {name} (state={state}, account={account}); "
                    "stop that container before using the account"
                )
    missing = set(identifiers) - inspected
    if result.returncode or missing:
        # An unrelated --rm container may disappear between ps and inspect.
        # Accept only omissions that a successful second listing confirms gone.
        current = set(_docker_read(list_command, "relist Docker containers").stdout.split())
        if not missing or missing & current:
            raise AccountLockError("Cannot inspect all Docker containers to check account use; retry when Docker is available")


def main(argv: list[str] | None = None) -> int:
    """Acquire a shell-owned FD so its lock lasts until the calling shell exits."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("account", type=Path)
    parser.add_argument("--kind", required=True, choices=("codex", "opencode"))
    parser.add_argument("--lock-fd", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        with account_lock(args.account, kind=args.kind, inherited_fd=args.lock_fd):
            ensure_account_not_in_use(args.account)
    except AccountLockError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
