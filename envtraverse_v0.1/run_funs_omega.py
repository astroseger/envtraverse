"""Omega account paths and native-runtime settings for the separate controller.

Scheduling, Docker networks, image ownership, and server lifecycle stay in
run_funs. Authentication directories are always mounted in their original place.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat

import run_funs as shared


@dataclass(frozen=True)
class RunPaths(shared.RunPaths):
    accounts_root: Path | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.accounts_root is not None:
            object.__setattr__(self, "accounts_root", self.accounts_root.expanduser().resolve())

    @property
    def run_config(self) -> Path:
        return self.root / "run_config_omega.yaml"

    @property
    def accounts_dir(self) -> Path:
        return self.accounts_root if self.accounts_root is not None else self.root / "omega_accounts"


def add_run_path_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-root", type=Path, default=shared.ROOT,
                        help=f"directory for outputs and default configuration; default: {shared.ROOT}")
    parser.add_argument("--run-config", type=Path,
                        help="configuration file; default: <run-root>/run_config_omega.yaml; "
                             "explicit relative paths use the caller's working directory")
    parser.add_argument("--omega-accounts-dir", dest="accounts_root", type=Path,
                        help="existing account directory; default: <run-root>/omega_accounts; "
                             "accounts are mounted here in place, never copied")


@dataclass(frozen=True)
class RunnerConfig:
    container_memory_bytes: int = 16 * 1024**3
    # Native Torch/Janus need more headroom than Codex's process-data default.
    process_data_soft_limit_bytes: int = 8 * 1024**3
    process_data_hard_limit_bytes: int = 8 * 1024**3


def parse_runner_config(config: dict[str, object], path: Path) -> RunnerConfig:
    settings = config.get("runner", {})
    if not isinstance(settings, dict):
        raise RuntimeError(f"runner in {path} must be a mapping")
    defaults = RunnerConfig()
    # Reuse shared validation while retaining Omega defaults for omitted fields.
    parsed = shared.parse_runner_config({**config, "runner": {
        "container_memory_gib": defaults.container_memory_bytes // 1024**3,
        "process_data_soft_limit_gib": defaults.process_data_soft_limit_bytes // 1024**3,
        "process_data_hard_limit_gib": defaults.process_data_hard_limit_bytes // 1024**3,
        **settings,
    }}, path)
    return RunnerConfig(
        container_memory_bytes=parsed.container_memory_bytes,
        process_data_soft_limit_bytes=parsed.process_data_soft_limit_bytes,
        process_data_hard_limit_bytes=parsed.process_data_hard_limit_bytes,
    )


def agent_memory_args(runner: RunnerConfig) -> list[str]:
    arguments = ["--memory", str(runner.container_memory_bytes),
                 "--memory-swap", str(runner.container_memory_bytes)]
    soft, hard = runner.process_data_soft_limit_bytes, runner.process_data_hard_limit_bytes
    if (soft is None) != (hard is None):
        raise ValueError("Process data soft and hard limits must both be set or both be absent")
    if soft is not None:
        arguments.extend(["--ulimit", f"data={soft}:{hard}"])
    return arguments


def validate_account(account: str, *, paths: RunPaths = RunPaths()) -> Path:
    """Validate file metadata without reading or copying credentials."""
    if not isinstance(account, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", account):
        raise RuntimeError("Omega account names must be directory names, without path separators")
    try:
        directory = (paths.accounts_dir / account).resolve(strict=True)
        directory_stat = directory.stat()
    except OSError as error:
        raise RuntimeError(f"Omega account folder does not exist or is inaccessible: {paths.accounts_dir / account}") from error
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise RuntimeError(f"Omega account path is not a directory: {directory}")
    if directory_stat.st_uid != os.getuid() or directory_stat.st_mode & 0o077:
        raise RuntimeError(f"Omega account directory must be private (0700) and owned by you: {directory}")
    if not os.access(directory, os.R_OK | os.W_OK | os.X_OK):
        raise RuntimeError(f"Omega account directory must be readable and writable for token refresh: {directory}")
    credential = directory / "chatgpt.json"
    try:
        credential_stat = credential.lstat()
    except OSError as error:
        raise RuntimeError(f"Omega account is missing chatgpt.json: {directory}") from error
    if not stat.S_ISREG(credential_stat.st_mode):
        raise RuntimeError(f"Omega chatgpt.json must be a regular file, not a symlink: {directory}")
    if credential_stat.st_uid != os.getuid() or credential_stat.st_mode & 0o077:
        raise RuntimeError(f"Omega chatgpt.json must be private (0600) and owned by you: {directory}")
    if not os.access(credential, os.R_OK):
        raise RuntimeError(f"Omega chatgpt.json is not readable: {directory}")
    if any(character in str(directory) for character in (",", '"', "\r", "\n", "\0")):
        raise RuntimeError("Omega account mount paths cannot contain commas, quotes, or control characters")
    return directory


def account_mount_args(account: str, *, paths: RunPaths = RunPaths()) -> list[str]:
    directory = validate_account(account, paths=paths)
    # Mount the directory RW: atomic token replacement and its lock must persist.
    return ["--mount", f"type=bind,source={directory},target=/home/user/.omega_auth"]
