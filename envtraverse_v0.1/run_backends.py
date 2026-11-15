"""Authentication and account lifecycle adapters for the shared experiment controller."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import re
import signal
import subprocess
from typing import TYPE_CHECKING

import run_funs as rf
from account_locks import account_lock, ensure_account_not_in_use
from opencode_chatgpt_accounts import account_tools
from opencode_usage import collect_account_usage

if TYPE_CHECKING:
    from run_controller import RunConfig

CODEX_CHATGPT = "codex_chatgpt_accounts"
OPENCODE_CHATGPT = "opencode_chatgpt_accounts"
CODEX_API_KEY = "codex_openai_api_key"
BACKENDS = frozenset({CODEX_CHATGPT, OPENCODE_CHATGPT, CODEX_API_KEY})
CHECK_CODEX_ACCOUNT_SCRIPT = rf.CODEX_ACCOUNT_SCRIPTS_DIR / "check_account.sh"
CLEAN_CODEX_ACCOUNT_SCRIPT = rf.CODEX_ACCOUNT_SCRIPTS_DIR / "clean_account.sh"
CHECK_CODEX_API_KEY_SCRIPT = rf.CODEX_ACCOUNT_SCRIPTS_DIR / "check_codex_with_api_key.sh"
CLEAN_OPENCODE_ACCOUNT_SCRIPT = rf.OPENCODE_ACCOUNT_SCRIPTS_DIR / "clean_account.sh"

API_KEY_WARNING = """\
Running with API keys is very expensive. The used version of codex have a picularity that with API key the percentage of input token wich hit the cache is very low (in my test run which cost me 300$ it was <20%), when you run it via ChatGPT PRO subsciption this percentage is much higher >95%.

Because of this what you can run with a weekly limit of a single ChatGPT PRO account, would probably cost you > 10000$ with API key.

So think twice before your run it.

If you are sure. Type continue
"""


def pending_usage_path(account: str, *, paths: rf.RunPaths = rf.RunPaths()) -> Path:
    return account_tools.pending_usage_path(paths.accounts_dir / account)


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def verify_no_pending_usage(accounts: list[str], *, paths: rf.RunPaths = rf.RunPaths()) -> None:
    for account in accounts:
        marker = pending_usage_path(account, paths=paths)
        if os.path.lexists(marker):
            raise RuntimeError(
                f"OpenCode account {account} has unsaved game usage: {marker}. "
                f"Preserve this account and stop its old container, then recover usage with "
                f"python3 {rf.ROOT / 'opencode_usage.py'} {paths.accounts_dir / account} "
                "before another check or cleanup."
            )


def register_game_usage(account: str, game_alias: str, game: str, container_name: str,
                        *, image: str, paths: rf.RunPaths) -> None:
    """Persist ownership before any online game process can use the clean DB."""
    marker = pending_usage_path(account, paths=paths)
    data = {"schema_version": 1, "account": str(paths.accounts_dir / account),
            "game_alias": game_alias, "game": game, "container_name": container_name,
            "output_path": str(paths.run_dir / game_alias / "run" / "opencode_usage.json"),
            "image": image}
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(data, output, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    fsync_directory(marker.parent)


def verify_opencode_accounts(config: RunConfig, *, agent_image: str) -> None:
    """Use each original mounted account directly; never export/copy credentials."""
    model = rf.resolve_model(config.agent_parameters, config.paths.run_config)
    command = ["opencode", "run", "--standalone", "--auto", "--format", "json", "--model", model]
    command += [account_tools.PING_PROMPT]
    docker = account_tools.Docker(agent_image, 120)
    for account in config.accounts:
        account_path = config.paths.accounts_dir / account
        rf.LOGGER.info("checking OpenCode account in place account=%s path=%s", account, account_path)
        output = docker.run(account_path, command, operation="OpenCode account live check")
        account_tools.validate_ping(output)
        rf.log_info("OpenCode account check passed: %s", account)


def clean_opencode_account(
    account: str, *, account_lock_fd: int, agent_image: str, paths: rf.RunPaths = rf.RunPaths(),
    on_cleaned: Callable[[], None] | None = None,
) -> None:
    """Reset history before a fresh game while retaining the controller's lock."""
    verify_no_pending_usage([account], paths=paths)
    if not CLEAN_OPENCODE_ACCOUNT_SCRIPT.is_file():
        raise RuntimeError(f"OpenCode account clean script does not exist: {CLEAN_OPENCODE_ACCOUNT_SCRIPT}")
    command = ["bash", str(CLEAN_OPENCODE_ACCOUNT_SCRIPT), str(paths.accounts_dir / account),
               "--lock-fd", str(account_lock_fd), "--image", agent_image]
    rf.log_info("cleaning OpenCode account %s before a fresh game", account)
    run_reserved_command(command, account_lock_fd, on_success=on_cleaned)


def run_reserved_command(command: list[str], account_lock_fd: int,
                         *, on_success: Callable[[], None] | None = None) -> None:
    """Wait for a lock-sharing helper to finish before honoring shutdown."""
    interrupted = False

    def defer_shutdown(signum: int, _frame: object) -> None:
        nonlocal interrupted
        interrupted = True
        rf.log_info("received signal %s; waiting for account operation before shutdown", signum)

    # subprocess.run kills its child when KeyboardInterrupt escapes. Let the
    # account helper finish or roll back before releasing locks or starting
    # another process. The scheduler's usual handler only sets a stop flag and
    # would otherwise allow this launch to continue after the cleaner exits.
    previous_handlers = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, defer_shutdown)
        try:
            subprocess.run(command, cwd=rf.ROOT, stdin=subprocess.DEVNULL,
                           pass_fds=(account_lock_fd,), start_new_session=True, check=True)
            if on_success is not None:
                on_success()
        except Exception:
            if interrupted:
                raise KeyboardInterrupt from None
            raise
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    if interrupted:
        raise KeyboardInterrupt


def run_reserved_live_command(command: list[str], account_lock_fd: int) -> None:
    """Interrupt an online helper, waiting for its container cleanup under lock."""
    process: subprocess.Popen | None = None
    interrupted = False
    termination_sent = False

    def terminate_helper() -> None:
        nonlocal termination_sent
        if process is not None and not termination_sent:
            termination_sent = True
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def interrupt_live_check(signum: int, _frame: object) -> None:
        nonlocal interrupted
        interrupted = True
        rf.log_info("received signal %s; stopping account live check before shutdown", signum)
        terminate_helper()

    previous_handlers = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, interrupt_live_check)
        try:
            process = subprocess.Popen(command, cwd=rf.ROOT, stdin=subprocess.DEVNULL,
                                       pass_fds=(account_lock_fd,), start_new_session=True)
            # A signal can arrive between spawning the helper and assigning its
            # handle. Stop it now in that case, then wait for the helper to
            # remove any named check container before the lock is released.
            if interrupted:
                terminate_helper()
            status = process.wait()
            if status:
                raise subprocess.CalledProcessError(status, command)
        except Exception:
            if interrupted:
                raise KeyboardInterrupt from None
            raise
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    if interrupted:
        raise KeyboardInterrupt


def verify_codex_api_key() -> None:
    if not CHECK_CODEX_API_KEY_SCRIPT.is_file():
        raise RuntimeError(f"codex API key check script does not exist: {CHECK_CODEX_API_KEY_SCRIPT}")
    if not os.access(CHECK_CODEX_API_KEY_SCRIPT, os.X_OK):
        raise RuntimeError(f"codex API key check script is not executable: {CHECK_CODEX_API_KEY_SCRIPT}")

    rf.LOGGER.info("checking codex API key with %s", CHECK_CODEX_API_KEY_SCRIPT)
    result = subprocess.run([str(CHECK_CODEX_API_KEY_SCRIPT)], cwd=rf.ROOT, check=False)
    if result.returncode != 0:
        rf.LOGGER.error("codex API key check failed exit_code=%s", result.returncode)
        raise RuntimeError(f"codex API key check failed with exit code {result.returncode}")
    rf.LOGGER.info("codex API key check passed")


def clean_codex_account(account: str, *, account_lock_fd: int, fast_service_tier: bool = False,
                        paths: rf.RunPaths = rf.RunPaths()) -> None:
    account_path = paths.accounts_dir / account
    rf.log_info("cleaning Codex account %s before a fresh game", account)
    run_reserved_command(["bash", str(CLEAN_CODEX_ACCOUNT_SCRIPT), str(account_path),
                          "--lock-fd", str(account_lock_fd)], account_lock_fd)
    if fast_service_tier:
        with (account_path / "config.toml").open("a", encoding="utf-8") as output:
            output.write('service_tier = "fast"\n')


def verify_codex_accounts(config: RunConfig, account_locks: dict[str, int]) -> None:
    for account in config.accounts:
        fd = account_locks[account]
        run_reserved_live_command(["bash", str(CHECK_CODEX_ACCOUNT_SCRIPT),
                                   str(config.paths.accounts_dir / account), "--lock-fd", str(fd)], fd)
        rf.log_info("Codex account check passed: %s", account)


def confirm_api_key_run() -> None:
    print(API_KEY_WARNING, flush=True)
    if input("> ") != "continue":
        raise RuntimeError("aborted")


class RunBackend:
    """Backend-owned authentication; scheduling and container lifecycle are shared."""
    account_kind: str | None = None
    log_command = True

    def __init__(self, config: RunConfig):
        self.config = config
        self.account_locks: dict[str, int] = {}

    def validate(self) -> None:
        is_opencode = self.config.agent.startswith("opencode_")
        if is_opencode != (self.config.backend == OPENCODE_CHATGPT):
            raise RuntimeError(f"agent {self.config.agent} is incompatible with backend {self.config.backend}")
        if self.account_kind is not None:
            accounts = self.config.accounts
            if not accounts or len(set(accounts)) != len(accounts):
                raise RuntimeError("account names must be nonempty and unique")
            for account in accounts:
                if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", account):
                    raise RuntimeError(f"invalid account name: {account!r}")
                directory = self.config.paths.accounts_dir / account
                if directory.is_symlink() or not directory.is_dir():
                    raise RuntimeError(f"account must be an existing directory, not a symlink: {directory}")

    def authorize(self) -> None:
        pass

    @contextmanager
    def reserve(self) -> Iterator[None]:
        with ExitStack() as cleanup:
            try:
                if self.account_kind is not None:
                    for account in sorted(self.config.accounts):
                        self.account_locks[account] = cleanup.enter_context(account_lock(
                            self.config.paths.accounts_dir / account, kind=self.account_kind,
                        ))
                    self.check_pending()
                    for account in self.config.accounts:
                        ensure_account_not_in_use(self.config.paths.accounts_dir / account)
                yield
            finally:
                self.account_locks.clear()

    def check_pending(self) -> None:
        pass

    def slots(self, game_count: int) -> list[str]:
        return self.config.accounts

    def preflight(self, image: str) -> None:
        raise NotImplementedError

    def prepare_game(self, slot: str, alias: str, game: str, container_name: str, image: str) -> None:
        pass

    def authentication_args(self, slot: str) -> list[str]:
        return []

    def process_environment(self) -> dict[str, str] | None:
        return None

    def finalize_game(self, slot: str, game: rf.RunningGame, image: str) -> None:
        pass


class CodexChatGPTBackend(RunBackend):
    account_kind = "codex"

    def preflight(self, image: str) -> None:
        verify_codex_accounts(self.config, self.account_locks)

    def prepare_game(self, slot: str, alias: str, game: str, container_name: str, image: str) -> None:
        clean_codex_account(slot, account_lock_fd=self.account_locks[slot],
                            fast_service_tier=self.config.codex_account_fast_service_tier,
                            paths=self.config.paths)

    def authentication_args(self, slot: str) -> list[str]:
        return ["-v", f"{self.config.paths.accounts_dir / slot}:/home/user/.codex"]


class OpenCodeChatGPTBackend(RunBackend):
    account_kind = "opencode"

    def __init__(self, config: RunConfig):
        super().__init__(config)
        self.owned_games: dict[str, tuple[str, str]] = {}

    def validate(self) -> None:
        super().validate()
        rf.resolve_model(self.config.agent_parameters, self.config.paths.run_config)
        rf.validate_accounts(self.config.accounts, paths=self.config.paths)

    def check_pending(self) -> None:
        verify_no_pending_usage(self.config.accounts, paths=self.config.paths)

    def preflight(self, image: str) -> None:
        verify_opencode_accounts(self.config, agent_image=image)

    def prepare_game(self, slot: str, alias: str, game: str, container_name: str, image: str) -> None:
        def account_cleaned() -> None:
            register_game_usage(slot, alias, game, container_name, image=image, paths=self.config.paths)
            self.owned_games[slot] = (alias, game)
        clean_opencode_account(slot, account_lock_fd=self.account_locks[slot], agent_image=image,
                               paths=self.config.paths, on_cleaned=account_cleaned)

    def authentication_args(self, slot: str) -> list[str]:
        return ["--init", "--mount", account_tools.bind_mount(self.config.paths.accounts_dir / slot)]

    def finalize_game(self, slot: str, game: rf.RunningGame, image: str) -> None:
        if self.owned_games.get(slot) != (game.alias, game.game):
            return
        paths = self.config.paths
        output = paths.run_dir / game.alias / "run" / "opencode_usage.json"
        try:
            collect_account_usage(paths.accounts_dir / slot, output, image=image,
                                  game_alias=game.alias, game=game.game)
        except Exception as exc:
            raise RuntimeError(
                f"could not save OpenCode usage for game {game.game}; account {slot} "
                f"was preserved with pending marker {pending_usage_path(slot, paths=paths)}"
            ) from exc
        marker = pending_usage_path(slot, paths=paths)
        marker.unlink()
        fsync_directory(marker.parent)
        del self.owned_games[slot]
        rf.log_info("saved OpenCode usage for game %s: %s", game.game, output)


class CodexAPIKeyBackend(RunBackend):
    log_command = False

    def authorize(self) -> None:
        confirm_api_key_run()
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set in the environment")

    def slots(self, game_count: int) -> list[str]:
        limit = self.config.max_parallel_games
        count = game_count if limit == "all" else min(game_count, limit)
        return [f"api-worker-{index}" for index in range(count)]

    def preflight(self, image: str) -> None:
        verify_codex_api_key()

    def authentication_args(self, slot: str) -> list[str]:
        # Docker inherits the value from its environment, keeping it out of argv.
        return ["-e", "CODEX_API_KEY"]

    def process_environment(self) -> dict[str, str]:
        return dict(os.environ, CODEX_API_KEY=os.environ["OPENAI_API_KEY"])


def create_backend(config: RunConfig) -> RunBackend:
    backend = {CODEX_CHATGPT: CodexChatGPTBackend, OPENCODE_CHATGPT: OpenCodeChatGPTBackend,
               CODEX_API_KEY: CodexAPIKeyBackend}[config.backend](config)
    return backend


