"""Prepare agent_v1's files and Docker launch arguments on the host."""

import math
from pathlib import Path
import shutil

from agent_api import AgentMount, LaunchPreparation
from .agent.client.client_storage import (
    DEFAULT_HTTP_READ_TIMEOUT_SECONDS, initialize_client_storage, positive_timeout_seconds,
)


AGENT_SOURCE_DIR = Path(__file__).resolve().parent / "agent"


def prepare_run(
    run_dir: Path,
    server_url: str,
    game_alias: str,
    parsed_run_config: dict[str, object],
    initial_files_dir: Path | None = None,
) -> LaunchPreparation:
    """Create this game's agent workspace without exposing the full run config."""
    model = parsed_run_config.get("model")
    reasoning_effort = parsed_run_config.get("reasoning_effort")
    for name, value in (("model", model), ("reasoning_effort", reasoning_effort)):
        if not isinstance(value, str) or not value:
            raise RuntimeError(f"{name} is required in run config and must be a non-empty string")

    recovery_delay_seconds = parsed_run_config.get("recovery_delay_seconds", 0)
    delay_error = "recovery_delay_seconds in run config must be a finite, non-negative number of seconds"
    if isinstance(recovery_delay_seconds, bool) or not isinstance(recovery_delay_seconds, (int, float)):
        raise RuntimeError(delay_error)
    try:
        recovery_delay_seconds = float(recovery_delay_seconds)
    except OverflowError as exc:
        raise RuntimeError(delay_error) from exc
    if not math.isfinite(recovery_delay_seconds) or recovery_delay_seconds < 0:
        raise RuntimeError(delay_error)

    max_recovery_attempts = parsed_run_config.get("max_recovery_attempts", 10)
    if (
        isinstance(max_recovery_attempts, bool)
        or not isinstance(max_recovery_attempts, int)
        or max_recovery_attempts < 0
    ):
        raise RuntimeError("max_recovery_attempts in run config must be a non-negative integer")

    timeouts = {}
    for name in ("http_read_timeout_seconds", "supervisor_inactivity_timeout_seconds"):
        if name in parsed_run_config:
            try:
                timeouts[name] = positive_timeout_seconds(parsed_run_config[name], name)
            except ValueError as exc:
                raise RuntimeError(f"{exc} in run config") from None

    command = [
        "python3", "agent_runner.py", game_alias,
        "--model", model, "--reasoning-effort", reasoning_effort,
    ]
    if recovery_delay_seconds > 0:
        command.extend(["--recovery-delay-seconds", str(recovery_delay_seconds)])
    if max_recovery_attempts != 10:
        command.extend(["--max-recovery-attempts", str(max_recovery_attempts)])
    if "supervisor_inactivity_timeout_seconds" in timeouts:
        command.extend(["--supervisor-inactivity-timeout-seconds",
                        str(timeouts["supervisor_inactivity_timeout_seconds"])])

    agent_run_dir = run_dir / "run"
    client_dir = run_dir / "client"
    observations_dir = run_dir / "observations"

    def ignore_agent_files(directory: str, names: list[str]) -> set[str]:
        ignored = {"__pycache__"}
        # Also skip leftovers (such as bytecode) at the former client location.
        if Path(directory) in (AGENT_SOURCE_DIR, AGENT_SOURCE_DIR / "workspace_init"):
            ignored.add("client")
        return ignored.intersection(names)

    shutil.copytree(AGENT_SOURCE_DIR, agent_run_dir, ignore=ignore_agent_files)
    if initial_files_dir is not None:
        shutil.copytree(initial_files_dir, agent_run_dir / "workspace_init", dirs_exist_ok=True)
    shutil.copytree(AGENT_SOURCE_DIR / "client", client_dir, ignore=shutil.ignore_patterns("__pycache__"))
    observations_dir.mkdir()
    # Host-created paths retain the host UID, matching the agent image's user.
    initialize_client_storage(
        client_dir, observations_dir,
        server_url=server_url,
        observations_path="/observations",
        http_read_timeout_seconds=timeouts.get("http_read_timeout_seconds", DEFAULT_HTTP_READ_TIMEOUT_SECONDS),
    )
    return LaunchPreparation(
        command=command,
        mounts=[
            # These mounts must be rw: the client temporarily toggles POSIX modes.
            AgentMount(client_dir, "/client"),
            AgentMount(observations_dir, "/observations"),
            AgentMount(agent_run_dir, "/home/user/run"),
        ],
    )
