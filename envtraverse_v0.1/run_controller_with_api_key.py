#!/usr/bin/env python3
"""Compatibility entry point for the shared Codex OpenAI API-key backend."""
from pathlib import Path
import run_controller as controller
import run_funs as rf
from run_backends import (
    CODEX_API_KEY, API_KEY_WARNING, CHECK_CODEX_API_KEY_SCRIPT,
    confirm_api_key_run, verify_codex_api_key,
)

RunConfig = controller.RunConfig


def parse_run_config(path: Path, *, paths=None):
    if paths is None:
        paths = rf.RunPaths(backend=CODEX_API_KEY)
    return controller.parse_run_config(path, paths=paths, forced_backend=CODEX_API_KEY)


def parse_args(argv):
    return controller.parse_args(argv)


def main(argv=None):
    return controller.main(argv, forced_backend=CODEX_API_KEY)


if __name__ == "__main__":
    raise SystemExit(main())
