#!/usr/bin/env python3
"""Compatibility entry point for the shared OpenCode ChatGPT backend."""
from pathlib import Path
import run_controller as controller
import run_funs as rf
from run_backends import (
    OPENCODE_CHATGPT, CLEAN_OPENCODE_ACCOUNT_SCRIPT, account_tools,
    clean_opencode_account, pending_usage_path, register_game_usage, verify_no_pending_usage,
    verify_opencode_accounts,
)

RunConfig = controller.RunConfig


def parse_run_config(path: Path, *, paths=None):
    if paths is None:
        paths = rf.RunPaths(backend=OPENCODE_CHATGPT, config_name="run_config_opencode.yaml")
    return controller.parse_run_config(path, paths=paths, forced_backend=OPENCODE_CHATGPT)


def parse_args(argv):
    return controller.parse_args(argv, config_name="run_config_opencode.yaml")


def main(argv=None):
    return controller.main(argv, forced_backend=OPENCODE_CHATGPT, config_name="run_config_opencode.yaml")


if __name__ == "__main__":
    raise SystemExit(main())
