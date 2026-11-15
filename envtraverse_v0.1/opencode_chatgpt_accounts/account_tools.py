"""Shared implementation for the shell entry points; requires Python 3.10+."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
import io
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS.parent))
import account_locks as shared_account_locks

DATA_PATH = "/home/user/.local/share/opencode"
CHATGPT_METHODS = {"chatgpt-headless", "chatgpt-browser"}
DEFAULT_MODEL = "openai/gpt-6.1-sol"
PING_PROMPT = (
    "Do not use tools. Your entire reply must be the four uppercase letters shown on the next line, "
    "with no punctuation, quotes, Markdown, or other text:\nPING"
)


class AccountError(Exception):
    def __init__(self, message: str, status: int = 1):
        super().__init__(message)
        self.status = status


def account_path(value: str) -> Path:
    path = Path(value).expanduser().absolute()
    if path.is_symlink():
        raise AccountError("The account directory must not be a symlink.")
    path = path.resolve()
    if path in {Path.home().resolve(), Path.cwd().resolve(), SCRIPTS, *SCRIPTS.parents}:
        raise AccountError("Use a dedicated account directory, not a home or project directory.")
    if path.exists() and (not path.is_dir() or path.is_mount()):
        raise AccountError("The account path must be a regular directory, not a file or mount point.")
    return path


def require_database(account: Path) -> None:
    database = account / "opencode.db"
    if not database.is_file() or database.is_symlink():
        raise AccountError(f"No OpenCode v2 database at {database}. Prepare this account first.")


def pending_usage_path(account: Path) -> Path:
    return account.parent / f".{account.name}.opencode.pending-usage.json"


def require_no_pending_usage(account: Path) -> None:
    marker = pending_usage_path(account)
    if marker.exists() or marker.is_symlink():
        raise AccountError(
            f"Account has uncollected game usage ({marker}). Preserve its database and run "
            f"python3 {SCRIPTS.parent / 'opencode_usage.py'} {account} before checking, preparing, or cleaning it."
        )


@contextmanager
def account_lock(account: Path, inherited_fd: int | None = None):
    try:
        with shared_account_locks.account_lock(account, kind="opencode", inherited_fd=inherited_fd) as fd:
            yield fd
    except shared_account_locks.AccountLockError as exc:
        raise AccountError(str(exc)) from None


def build_image() -> str:
    result = subprocess.run(
        ["bash", str(SCRIPTS / "build_docker.sh"), "--if-missing"],
        stdout=subprocess.PIPE, text=True,
    )
    if result.returncode:
        raise AccountError("Could not build/find the account-tools image.", result.returncode)
    image = result.stdout.strip()
    if not image or any(char.isspace() for char in image):
        raise AccountError("The image builder returned an invalid image name.")
    return image


def bind_mount(account: Path) -> str:
    # Docker parses --mount as CSV; quoting also supports commas in host paths.
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="").writerow(
        ["type=bind", f"source={account}", f"target={DATA_PATH}"]
    )
    return buffer.getvalue()


class Docker:
    def __init__(self, image: str, timeout: float):
        self.image = image
        self.timeout = timeout

    def run(self, account: Path, command: list[str], *, operation: str,
            output=None, input_file=None, login=False, offline=False, shell=False, isolate=False):
        name = f"opencode-account-{uuid.uuid4().hex}"
        args = ["docker", "run", "--rm", "--init", "--pull=never", "--name", name,
                "--mount", bind_mount(account)]
        if login or input_file is not None:
            args.append("--interactive")
        if login and sys.stdin.isatty() and sys.stdout.isatty():
            args.append("--tty")
        if offline:
            args += ["--network", "none"]
        args += [self.image, "bash", "-ec"]
        if shell:
            args += ["umask 077; " + command[0]]
        else:
            args += ['umask 077; exec "$@"', "opencode-account", *command]
        try:
            result = subprocess.run(
                args, stdin=input_file if input_file is not None else (None if login else subprocess.DEVNULL),
                stdout=None if login else (output if output is not None else subprocess.PIPE),
                stderr=None if login else subprocess.PIPE, timeout=self.timeout,
                start_new_session=isolate,
            )
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            # Killing the Docker client alone does not stop the container/server.
            try:
                stopped = subprocess.run(
                    ["docker", "rm", "--force", name], stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=15,
                )
            except (OSError, subprocess.TimeoutExpired):
                stopped = None
            if stopped is None or stopped.returncode:
                raise AccountError(f"{operation} interrupted; could not stop container {name}. "
                                   "Stop it before using this account again.") from None
            if isinstance(error, KeyboardInterrupt):
                raise
            raise AccountError(f"{operation} timed out after {self.timeout:g}s.", 124) from None
        if result.returncode:
            # In particular, never echo a failed credential export/import's output.
            raise AccountError(f"{operation} failed (exit {result.returncode}). "
                               f"OpenCode diagnostics, if available: {account / 'log'}.",
                               result.returncode if result.returncode > 0 else 1)
        return result.stdout


def read_credentials(path: Path) -> list[dict]:
    try:
        credentials = json.loads(path.read_text())
    except (ValueError, UnicodeError):
        raise AccountError("Credential export is not valid JSON; account was not replaced.") from None
    valid = isinstance(credentials, list) and bool(credentials)
    ids, active = set(), set()
    if valid:
        for entry in credentials:
            if not isinstance(entry, dict) or not all(
                isinstance(entry.get(key), str) for key in ("id", "integrationID", "label")
            ) or not isinstance(entry.get("active"), bool) or not isinstance(entry.get("value"), dict):
                valid = False
                break
            if not entry["id"] or not entry["integrationID"] or entry["id"] in ids:
                valid = False
                break
            ids.add(entry["id"])
            if entry["active"]:
                if entry["integrationID"] in active:
                    valid = False
                    break
                active.add(entry["integrationID"])
            value = entry["value"]
            kind = value.get("type")
            if not isinstance(kind, str):
                valid = False
                break
            fields = {"oauth": ("methodID", "refresh", "access"), "key": ("key",),
                      "external": ("methodID",)}.get(kind)
            if fields is None or not all(isinstance(value.get(key), str) and value[key] for key in fields):
                valid = False
                break
            if kind == "oauth" and (type(value.get("expires")) is not int or value["expires"] < 0):
                valid = False
                break
            if "metadata" in value and not isinstance(value["metadata"], dict):
                valid = False
                break
    if not valid:
        raise AccountError("Credential export is empty or has an unsupported structure; account was not replaced.")
    if not any(entry["integrationID"] == "openai" and entry["active"]
               and entry["value"]["type"] == "oauth"
               and entry["value"]["methodID"] in CHATGPT_METHODS for entry in credentials):
        raise AccountError("No active OpenAI ChatGPT OAuth credential. An API key is not a ChatGPT login.")
    return sorted(credentials, key=lambda entry: entry["id"])


def export_credentials(docker: Docker, account: Path, destination: Path) -> list[dict]:
    with destination.open("xb") as output:
        docker.run(account, ["opencode", "auth", "export", "--standalone"],
                   operation="Credential export", output=output, offline=True)
    return read_credentials(destination)


def prepare(docker: Docker, account: Path) -> None:
    account.mkdir(mode=0o700, exist_ok=True)
    print("Complete the ChatGPT device login shown below.", flush=True)
    docker.run(account, ["opencode", "auth", "login", "openai", "--standalone",
                        "--method", "chatgpt-headless"], operation="ChatGPT login", login=True)
    with tempfile.TemporaryDirectory(prefix=f".{account.name}.verify-", dir=account.parent) as temporary:
        export_credentials(docker, account, Path(temporary) / "credentials.json")
    print(f"Account prepared: {account}")


def validate_ping(output: bytes) -> None:
    try:
        events = [json.loads(line) for line in output.decode().splitlines() if line.strip()]
    except (ValueError, UnicodeError):
        raise AccountError("Live check returned invalid JSON output.") from None
    if not events or not all(isinstance(event, dict) for event in events):
        raise AccountError("Live check returned no valid events.")
    if any(event.get("type") == "error" for event in events):
        raise AccountError("OpenCode reported a model error during the live check.")
    texts = []
    for event in events:
        if event.get("type") == "text":
            part = event.get("part")
            if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                raise AccountError("Live check returned an invalid text event.")
            texts.append(part["text"])
    reply = "".join(texts).strip()
    if reply != "PING":
        preview = repr(reply[:160]) + (" (truncated)" if len(reply) > 160 else "")
        raise AccountError(f"Live check did not return exactly PING; received {preview}.")


def check(docker: Docker, account: Path, model: str) -> None:
    with tempfile.TemporaryDirectory(prefix=f".{account.name}.check-", dir=account.parent) as temporary:
        export_credentials(docker, account, Path(temporary) / "credentials.json")
    print(f"Running a live check with {model}...", file=sys.stderr, flush=True)
    output = docker.run(account, ["opencode", "run", "--standalone", "--auto", "--format", "json",
                                  "--model", model, PING_PROMPT],
                        operation="Live check")
    validate_ping(output)
    print(f"Account OK: ChatGPT returned PING ({model}).")


def clean(docker: Docker, account: Path) -> None:
    transaction = Path(tempfile.mkdtemp(prefix=f".{account.name}.clean-", dir=account.parent))
    backup = transaction / "original"
    try:
        exported = transaction / "credentials.json"
        credentials = export_credentials(docker, account, exported)
        fresh = transaction / "fresh"
        fresh.mkdir(mode=0o700)
        restored = transaction / "restored.json"
        # One container imports from stdin, then re-exports for exact comparison.
        with exported.open("rb") as source, restored.open("xb") as output:
            docker.run(fresh, ["opencode auth import --standalone; exec opencode auth export --standalone"],
                       operation="Credential restore", input_file=source, output=output, offline=True, shell=True)
        if read_credentials(restored) != credentials:
            raise AccountError("Restored credentials differ from the export; original account was not replaced.")
        require_database(fresh)
        # Containers have exited. Both directories share a filesystem. Keep the
        # original until the verified replacement has been moved into place.
        account.rename(backup)
        try:
            fresh.rename(account)
        except BaseException:
            backup.rename(account)
            raise
        shutil.rmtree(backup)
    finally:
        if backup.exists():
            # Never destroy the only copy if a rename/rollback or deletion fails.
            print(f"Original account retained for recovery: {backup}", file=sys.stderr)
        else:
            shutil.rmtree(transaction)
    print(f"Account cleaned; credentials preserved and conversation history removed: {account}")


def positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Timeout must be a positive number of seconds.") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("Timeout must be a positive number of seconds.")
    return seconds


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage isolated OpenCode v2 ChatGPT accounts.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "check", "clean"):
        command = commands.add_parser(name)
        command.add_argument("account", help="host directory mounted at " + DATA_PATH)
        command.add_argument("--timeout", type=positive_seconds, default=900 if name == "prepare" else 120,
                             help="seconds allowed per container (default: %(default)s)")
        if name == "prepare":
            command.add_argument("--resume", action="store_true", help="retry login in an existing account directory")
        elif name == "check":
            command.add_argument("--model", default=os.environ.get("OPENCODE_MODEL", DEFAULT_MODEL),
                                 help="OpenAI model for the live request (or OPENCODE_MODEL)")
        elif name == "clean":
            command.add_argument("--lock-fd", type=int,
                                 help="inherited descriptor for this account's controller-held lock")
            command.add_argument("--image", help="use an existing image instead of building the account-tools image")
    args = parser.parse_args()
    os.umask(0o077)

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        account = account_path(args.account)
        if args.command == "check" and (not args.model.startswith("openai/")
                                         or not args.model.removeprefix("openai/")
                                         or any(char.isspace() for char in args.model)):
            raise AccountError("The live-check model must have the form openai/<model>.", 2)
        image = getattr(args, "image", None)
        if image is not None and (not image or any(char.isspace() for char in image)):
            raise AccountError("The image name must be nonempty and contain no whitespace.", 2)
        if args.command == "prepare":
            account.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        elif not account.is_dir():
            raise AccountError(f"Account directory does not exist: {account}")
        with account_lock(account, inherited_fd=getattr(args, "lock_fd", None)):
            require_no_pending_usage(account)
            shared_account_locks.ensure_account_not_in_use(account)
            if args.command == "prepare":
                if account.exists() and not args.resume:
                    raise AccountError("Account already exists. Use --resume to retry its ChatGPT login.")
            else:
                require_database(account)
            docker = Docker(image if image is not None else build_image(), args.timeout)
            if args.command == "prepare":
                prepare(docker, account)
            elif args.command == "check":
                check(docker, account, args.model)
            else:
                clean(docker, account)
    except AccountError as error:
        print(f"Error: {error}", file=sys.stderr)
        return error.status
    except OSError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Account data was retained; use prepare_account.sh --resume to retry a login.",
              file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
