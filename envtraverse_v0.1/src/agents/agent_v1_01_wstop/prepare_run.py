"""Prepare agent_v1's files and Docker launch arguments on the host."""

from pathlib import Path
import shutil

from agent_api import AgentMount, LaunchPreparation
from .agent.client.client_storage import initialize_client_storage


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
    )
    return LaunchPreparation(
        command=[
            "python3", "agent_runner.py", game_alias,
            "--model", model, "--reasoning-effort", reasoning_effort,
        ],
        mounts=[
            # These mounts must be rw: the client temporarily toggles POSIX modes.
            AgentMount(client_dir, "/client"),
            AgentMount(observations_dir, "/observations"),
            AgentMount(agent_run_dir, "/home/user/run"),
        ],
    )
