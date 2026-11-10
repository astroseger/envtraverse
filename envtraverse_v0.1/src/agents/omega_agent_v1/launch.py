#!/usr/bin/env python3
"""Prepare a fresh task and exec Docker; no continuation/recovery supervisor."""

import argparse
import importlib
import os
from pathlib import Path
import shlex
import subprocess
import sys


PACKAGE_DIR = Path(__file__).resolve().parent
ROOT = PACKAGE_DIR.parents[2]


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def mount_argument(source, target, read_only=False):
    value = str(source)
    if any(character in value for character in (",", '"', "\n", "\r")):
        raise ValueError("Mount paths cannot contain commas, double quotes, or line breaks")
    return f"type=bind,source={value},target={target}" + (",readonly" if read_only else "")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", required=True, help="Running environment server reachable from this Linux host")
    parser.add_argument("--game", required=True, help="Public game/task alias accepted by the server")
    parser.add_argument("--account", required=True, type=Path, help="Omega account directory containing chatgpt.json")
    parser.add_argument("--run-dir", required=True, type=Path, help="Fresh task output directory")
    parser.add_argument("--model", required=True, help="Model listed by the Omega account")
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--max-new-input-loops", type=positive_int, default=10_000_000)
    parser.add_argument("--http-read-timeout-seconds", type=float, default=3900)
    parser.add_argument("--initial-files", type=Path, help="Public initial workspace files")
    args = parser.parse_args(argv)
    try:
        if not args.model.strip() or not args.reasoning_effort.strip():
            raise ValueError("Model and reasoning effort must be non-empty")
        sys.path.insert(0, str(ROOT))
        prepare = importlib.import_module("src.agents.omega_agent_v1.prepare_run").prepare_run
        storage = importlib.import_module("src.agents.omega_agent_v1.agent.client.client_storage")
        # Validate before a potentially expensive build or any task-directory writes.
        storage.ClientConfig.from_dict({
            "server_url": args.server_url, "observations_path": "/observations",
            "http_read_timeout_seconds": args.http_read_timeout_seconds,
        })
        account = args.account.expanduser().resolve(strict=True)
        if not account.is_dir() or account.stat().st_uid != os.getuid():
            raise ValueError("The account directory must be owned by the current user")
        credential = account / "chatgpt.json"
        if (credential.is_symlink() or not credential.is_file()
                or credential.stat().st_uid != os.getuid() or credential.stat().st_mode & 0o077):
            raise ValueError("Expected chatgpt.json owned by you with private (0600) permissions")
        auth_mount = mount_argument(account, "/home/user/.omega_auth")
        run_dir = args.run_dir.expanduser().absolute()
        if run_dir.exists() or run_dir.is_symlink():
            raise FileExistsError(f"Use a fresh output directory: {run_dir}")
        mount_argument(run_dir, "/home/user/run")
        if args.initial_files is not None and not args.initial_files.is_dir():
            raise FileNotFoundError(f"Initial files directory does not exist: {args.initial_files}")
        image = subprocess.check_output(
            ["bash", str(PACKAGE_DIR / "build_docker.sh"), "--if-missing"], text=True,
        ).strip()
        if not image or any(character.isspace() for character in image):
            raise RuntimeError("Image builder returned an invalid image reference")
        preparation = prepare(
            run_dir, args.server_url, args.game,
            {"model": args.model, "reasoning_effort": args.reasoning_effort,
             "max_new_input_loops": args.max_new_input_loops,
             "http_read_timeout_seconds": args.http_read_timeout_seconds},
            initial_files_dir=args.initial_files,
        )
        command = ["docker", "run", "--rm", "--pull=never", "--init", "--network", "host",
                   "--mount", auth_mount]
        for mount in preparation.mounts:
            command.extend(["--mount", mount_argument(mount.source.absolute(), mount.target, mount.read_only)])
        for key, value in preparation.environment.items():
            command.extend(["--env", f"{key}={value}"])
        command.extend([image, *preparation.command])
        print(f"Starting Omega: {shlex.join(command)}", file=sys.stderr, flush=True)
        os.execvp(command[0], command)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Omega launch failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
