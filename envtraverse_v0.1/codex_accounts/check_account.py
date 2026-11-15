"""Interruptible live Codex check; the shell/controller retains its account lock."""
from __future__ import annotations

import argparse
from pathlib import Path
import signal
import subprocess
import sys
import uuid

if __package__:
    from .account_locks import AccountLockError, account_lock, ensure_account_not_in_use
else:
    from account_locks import AccountLockError, account_lock, ensure_account_not_in_use


class CheckError(RuntimeError):
    def __init__(self, message: str, status: int = 1):
        super().__init__(message)
        self.status = status


def stop_container(name: str) -> None:
    """Finish bounded cleanup despite repeated terminal interrupts."""
    previous = {number: signal.signal(number, signal.SIG_IGN) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        try:
            removed = subprocess.run(["docker", "rm", "--force", name],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            if removed.returncode == 0:
                return
            # --rm may already have removed a container after its client exits.
            remaining = subprocess.run(["docker", "ps", "--all", "--format", "{{.Names}}"],
                                       capture_output=True, text=True, timeout=15)
            if remaining.returncode == 0 and name not in remaining.stdout.splitlines():
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
        raise CheckError(f"could not stop account-check container {name}; stop it before using this account again")
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def run_check(account: Path, image: str, arguments: list[str], label: str) -> str:
    name = f"codex-account-check-{uuid.uuid4().hex}"
    command = ["docker", "run", "--rm", "--pull=never", "--name", name,
               "-v", f"{account}:/home/user/.codex", image, "codex", *arguments]
    try:
        # subprocess.run kills and waits for its Docker client before propagating
        # KeyboardInterrupt; the daemon's named container is removed afterwards.
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except KeyboardInterrupt:
        stop_container(name)
        raise
    if result.returncode:
        stop_container(name)
        print(f"Error: {label} command failed with exit code {result.returncode}", file=sys.stderr)
        print(result.stdout.rstrip("\n"), file=sys.stderr)
        raise CheckError("", result.returncode if result.returncode > 0 else 1)
    return result.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("account")
    parser.add_argument("image")
    parser.add_argument("--lock-fd", type=int, required=True)
    args = parser.parse_args(argv)

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    previous = {number: signal.signal(number, interrupted) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        account = Path(args.account).expanduser()
        with account_lock(account, kind="codex", inherited_fd=args.lock_fd):
            account = account.resolve()
            ensure_account_not_in_use(account)
            login = run_check(account, args.image, ["login", "status"], "codex login status")
            if "Logged in using ChatGPT" not in login:
                print("Error: login status did not report ChatGPT login", file=sys.stderr)
                print(login.rstrip("\n"), file=sys.stderr)
                return 1
            output = run_check(account, args.image,
                               ["exec", "--skip-git-repo-check", "Respond with exactly: PING"], "codex exec PING")
            if "PING" not in output.split("\n"):
                print("Error: codex exec did not return PING", file=sys.stderr)
                print(output.rstrip("\n"), file=sys.stderr)
                return 1
        print(f"Account OK: {args.account}")
        return 0
    except (CheckError, AccountLockError, OSError) as exc:
        if str(exc):
            print(f"Error: {exc}", file=sys.stderr)
        return exc.status if isinstance(exc, CheckError) else 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())
