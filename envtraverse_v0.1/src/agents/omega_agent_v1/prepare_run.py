"""Prepare one fresh, unsupervised Omega task using agent_v1's unchanged client."""

from pathlib import Path
import shutil

from agent_api import AgentMount, LaunchPreparation
from .agent.client.client_storage import (
    ClientConfig, DEFAULT_HTTP_READ_TIMEOUT_SECONDS, initialize_client_storage, positive_timeout_seconds,
)


AGENT_SOURCE_DIR = Path(__file__).resolve().parent / "agent"


def prepare_run(
    run_dir: Path,
    server_url: str,
    game_alias: str,
    parsed_run_config: dict[str, object],
    initial_files_dir: Path | None = None,
) -> LaunchPreparation:
    """Copy only public task inputs; the account is mounted by the launcher."""
    model = parsed_run_config.get("model")
    reasoning_effort = parsed_run_config.get("reasoning_effort")
    for name, value in (("model", model), ("reasoning_effort", reasoning_effort)):
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"{name} is required in run config and must be a non-empty string")
    cycles = parsed_run_config.get("max_new_input_loops", 10_000_000)
    if type(cycles) is not int or cycles <= 0:
        raise RuntimeError("max_new_input_loops must be a positive integer")
    try:
        http_timeout = positive_timeout_seconds(
            parsed_run_config.get("http_read_timeout_seconds", DEFAULT_HTTP_READ_TIMEOUT_SECONDS),
            "http_read_timeout_seconds",
        )
    except ValueError as error:
        raise RuntimeError(str(error)) from None

    ClientConfig.from_dict({"server_url": server_url, "observations_path": "/observations",
                            "http_read_timeout_seconds": http_timeout})
    run_dir = Path(run_dir)
    agent_run_dir = run_dir / "run"
    client_dir = run_dir / "client"
    observations_dir = run_dir / "observations"
    memory_dir = run_dir / "omega_memory"
    for path in (agent_run_dir, client_dir, observations_dir, memory_dir):
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"Fresh Omega run requires this path to be absent: {path}")
    if initial_files_dir is not None and not Path(initial_files_dir).is_dir():
        raise FileNotFoundError(f"Initial files directory does not exist: {initial_files_dir}")

    def ignore_agent_files(directory: str, names: list[str]) -> set[str]:
        ignored = {"__pycache__"}
        if Path(directory) in (AGENT_SOURCE_DIR, AGENT_SOURCE_DIR / "workspace_init"):
            ignored.add("client")
        return ignored.intersection(names)

    shutil.copytree(AGENT_SOURCE_DIR, agent_run_dir, ignore=ignore_agent_files)
    if initial_files_dir is not None:
        shutil.copytree(initial_files_dir, agent_run_dir / "workspace_init", dirs_exist_ok=True)
    shutil.copytree(AGENT_SOURCE_DIR / "client", client_dir, ignore=shutil.ignore_patterns("__pycache__"))
    observations_dir.mkdir()
    memory_dir.mkdir()
    initialize_client_storage(
        client_dir, observations_dir,
        server_url=server_url, observations_path="/observations",
        http_read_timeout_seconds=http_timeout,
    )
    return LaunchPreparation(
        command=["python3", "agent.py", "--master", game_alias,
                 "--model", model, "--reasoning-effort", reasoning_effort,
                 "--max-new-input-loops", str(cycles)],
        mounts=[
            AgentMount(client_dir, "/client"),
            AgentMount(observations_dir, "/observations"),
            AgentMount(agent_run_dir, "/home/user/run"),
            AgentMount(memory_dir, "/PeTTa/repos/Omega/memory"),
        ],
        environment={
            "CHROMA_DB_PATH": "/PeTTa/repos/Omega/memory/chroma_db",
            # The image already contains the local embedding model. Controllers
            # restrict outbound traffic to OpenAI; no model download is needed.
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "ANONYMIZED_TELEMETRY": "False",
            # The unchanged PeTTa run.sh retains a shell parent for SWI-Prolog.
            "TINI_KILL_PROCESS_GROUP": "1",
        },
    )
