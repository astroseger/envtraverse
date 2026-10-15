from __future__ import annotations

import importlib
import logging
import os
import re
import subprocess
import time
import json
import signal
import stat
import sys
from collections import deque
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence
from uuid import uuid4

import yaml

from agent_api import LaunchPreparation


ROOT = Path(__file__).resolve().parent
RUN_CONFIG = ROOT / "run_config.yaml"
RUN_DIR = ROOT / "run"
RUNNER_LOG_PATH = RUN_DIR / "runner.log"
AGENTS_DIR = ROOT / "src" / "agents"
ENVIRONMENTS_DIR = ROOT / "src" / "server" / "environments"
CODEX_ACCOUNTS_DIR = ROOT / "codex_accounts"
BUILD_DOCKERS_SCRIPT = ROOT / "src" / "build_dockers.sh"

SERVER_PORT = 8879
SERVER_START_TIMEOUT_SECONDS = 1800.0
PROXY_PORT = 3128
# Limit each process's data allocations without restricting the code-mode host's
# large virtual address reservations. The container cap is an aggregate backstop.
AGENT_PROCESS_DATA_LIMIT_BYTES = 2 * 1024**3
AGENT_MEMORY_LIMIT = "10g"
LOGGER = logging.getLogger("arc-runner")
SERVER_MANAGED_VARIABLES = frozenset({"ENV_SERVER_HOST", "ENV_SERVER_PORT"})


@dataclass(frozen=True)
class ServerMount:
    source: Path
    target: str


@dataclass(frozen=True)
class EnvironmentConfig:
    name: str
    parameters: dict[str, Any]
    variables: list[str]
    mounts: tuple[ServerMount, ...] = ()


@dataclass(frozen=True)
class RunImages:
    agent: str
    server: str
    proxy: str

    @classmethod
    def create(cls) -> RunImages:
        run_id = uuid4().hex
        return cls(
            agent=f"unified-agent:run-{run_id}",
            server=f"unified-server:run-{run_id}",
            proxy=f"unified-proxy:run-{run_id}",
        )


@dataclass(frozen=True)
class ContainerNames:
    internal_network: str
    internet_network: str
    proxy: str
    servers: dict[str, str]
    agents: dict[str, str]


@dataclass
class RunningContainer:
    container_name: str
    process: subprocess.Popen[bytes]


@dataclass
class RunningGame:
    game: str
    alias: str
    server: RunningContainer
    startup_deadline: float
    agent: RunningContainer | None = None


def set_logger_name(name: str) -> None:
    global LOGGER
    LOGGER = logging.getLogger(name)


def setup_runner_logging() -> None:
    if RUNNER_LOG_PATH.exists():
        raise RuntimeError(f"runner log already exists: {RUNNER_LOG_PATH}")

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    file_handler = logging.FileHandler(RUNNER_LOG_PATH, mode="x", encoding="utf-8")
    file_handler.setFormatter(formatter)

    LOGGER.handlers.clear()
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(file_handler)
    LOGGER.propagate = False


def log_info(message: str, *args: object) -> None:
    text = message % args if args else message
    print(text, flush=True)
    LOGGER.info(message, *args)


def load_run_config(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise RuntimeError(f"run config does not exist: {path}")

    with path.open("r", encoding="utf-8") as config_file:
        loaded_config = yaml.safe_load(config_file)

    if not isinstance(loaded_config, dict):
        raise RuntimeError(f"{path} must contain a YAML mapping")

    return loaded_config


def parse_string_list(config: dict[str, object], key: str, path: Path) -> list[str]:
    value = config.get(key)
    if not isinstance(value, list) or not value:
        raise RuntimeError(f"{key} in {path} must be a non-empty list")
    if not all(isinstance(item, str) and item for item in value):
        raise RuntimeError(f"{key} in {path} must contain only non-empty strings")
    return value


def agent_directory(name: str) -> Path:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise RuntimeError("agent must be a package name containing ASCII letters, digits, or underscores")
    directory = AGENTS_DIR / name
    if directory.resolve().parent != AGENTS_DIR.resolve() or not all(
        (directory / filename).is_file() for filename in ("Dockerfile", "prepare_run.py")
    ):
        raise RuntimeError(f"agent package does not exist: {name}")
    return directory


def parse_agent_name(config: dict[str, object], path: Path) -> str:
    name = config.get("agent")
    if not isinstance(name, str) or not name:
        raise RuntimeError(f"agent in {path} must be a non-empty string")
    agent_directory(name)
    return name


def environment_directory(name: str) -> Path:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise RuntimeError("environment must be a package name containing ASCII letters, digits, or underscores")
    directory = ENVIRONMENTS_DIR / name
    if directory.resolve().parent != ENVIRONMENTS_DIR.resolve() or not (directory / "environment_manager.py").is_file():
        raise RuntimeError(f"environment package does not exist: {name}")
    return directory


def parse_environment_config(config: dict[str, object], path: Path) -> EnvironmentConfig:
    for key in ("competition", "tag"):
        if key in config:
            raise RuntimeError(f"move {key} in {path} into environment_parameters")
    if "environmnet_parameters" in config:
        raise RuntimeError(f"use environment_parameters in {path}, not environmnet_parameters")

    name = config.get("environment")
    if not isinstance(name, str) or not name:
        raise RuntimeError(f"environment in {path} must be a non-empty string")
    environment_directory(name)

    parameters = config.get("environment_parameters", {})
    if not isinstance(parameters, dict):
        raise RuntimeError(f"environment_parameters in {path} must be a JSON-compatible mapping")
    try:
        # A round trip also rejects non-string mapping keys that JSON would silently convert.
        serialized_parameters = json.dumps(parameters, allow_nan=False)
        if json.loads(serialized_parameters) != parameters:
            raise ValueError("parameters changed during JSON serialization")
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise RuntimeError(
            f"environment_parameters in {path} must contain JSON values with string mapping keys"
        ) from None

    variables = config.get("environment_variables", [])
    if not isinstance(variables, list):
        raise RuntimeError(f"environment_variables in {path} must be a list of variable names")
    seen_variables: set[str] = set()
    for name_in_environment in variables:
        if not isinstance(name_in_environment, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", name_in_environment
        ):
            # Do not echo malformed entries: they might contain an inline secret.
            raise RuntimeError(f"environment_variables in {path} must contain names only, not assignments or values")
        if name_in_environment in SERVER_MANAGED_VARIABLES:
            raise RuntimeError(f"environment variable {name_in_environment} is managed by the launcher")
        if name_in_environment in seen_variables:
            raise RuntimeError(f"duplicate environment variable: {name_in_environment}")
        seen_variables.add(name_in_environment)

    if name == "terminal_bench" and "session_id" in parameters:
        raise RuntimeError("Terminal-Bench session_id is assigned separately to each game by the controller")

    mounts = []
    raw_mounts = config.get("server_mounts", [])
    if not isinstance(raw_mounts, list):
        raise RuntimeError(f"server_mounts in {path} must be a list of source/target mappings")
    targets = {"/data"}
    for entry in raw_mounts:
        if not isinstance(entry, dict) or set(entry) != {"source", "target"}:
            raise RuntimeError("Each server_mounts entry must contain exactly source and target")
        source, target = entry["source"], entry["target"]
        if any(not isinstance(value, str) or not value or any(c in value for c in ",\0\r\n")
               for value in (source, target)):
            raise RuntimeError("Mount source and target must be nonempty paths without commas or control characters")
        target_path = Path(target)
        target = "/" + str(target_path).lstrip("/")
        if not target_path.is_absolute() or ".." in target_path.parts or target == "/":
            raise RuntimeError("Mount target must be an absolute container directory other than /")
        if target in targets:
            raise RuntimeError(f"Duplicate or reserved server mount target: {target}")
        targets.add(target)
        source_path = (ROOT / source).resolve()
        if not source_path.is_dir():
            message = f"Server mount source directory does not exist: {source_path}"
            if name == "terminal_bench":
                message += (
                    "\nPlease run the host runtime first in a separate terminal: "
                    "src/server/environments/terminal_bench/host_runtime/run.sh"
                )
            raise RuntimeError(message)
        mounts.append(ServerMount(source_path, target))

    return EnvironmentConfig(name=name, parameters=parameters, variables=variables, mounts=tuple(mounts))


def verify_environment_variables(environment: EnvironmentConfig) -> None:
    missing = [name for name in environment.variables if name not in os.environ]
    if missing:
        raise RuntimeError(f"required environment variables are not set: {', '.join(missing)}")


def validate_unique_games(games: list[str]) -> None:
    seen_games: set[str] = set()
    duplicate_games: list[str] = []
    for game in games:
        if game in seen_games:
            duplicate_games.append(game)
        seen_games.add(game)
    if duplicate_games:
        duplicates = " ".join(sorted(set(duplicate_games)))
        raise RuntimeError(f"games must be unique; duplicates: {duplicates}")


def prepare_run_root() -> None:
    if RUN_DIR.exists():
        raise RuntimeError(f"run folder already exists: {RUN_DIR}")
    RUN_DIR.mkdir()


def make_run_writable() -> None:
    """Restore owner writes after shutdown, without following symlinks."""
    def report_error(exc: OSError) -> None:
        LOGGER.warning("could not restore run write permissions: %s", exc)
        print(f"warning: could not restore run write permissions: {exc}", file=sys.stderr)

    def add_owner_write(path: Path) -> None:
        try:
            mode = path.lstat().st_mode
            if not stat.S_ISLNK(mode) and not mode & stat.S_IWUSR:
                path.chmod(stat.S_IMODE(mode) | stat.S_IWUSR, follow_symlinks=False)
        except OSError as exc:
            report_error(exc)

    try:
        if RUN_DIR.is_symlink() or not RUN_DIR.is_dir():
            return
    except OSError as exc:
        report_error(exc)
        return

    log_info("restoring owner write permission in %s", RUN_DIR)
    add_owner_write(RUN_DIR)
    for directory, directories, files in os.walk(RUN_DIR, followlinks=False, onerror=report_error):
        for name in directories + files:
            add_owner_write(Path(directory) / name)


def initial_workspace_files(environment_name: str) -> Path | None:
    source_dir = environment_directory(environment_name) / "to_agent_workspace"
    if not source_dir.exists():
        return None
    if not source_dir.is_dir():
        raise RuntimeError(f"server-to-agent workspace path is not a folder: {source_dir}")
    return source_dir


def prepare_game_directory(game_alias: str) -> Path:
    game_dir = RUN_DIR / game_alias
    game_dir.mkdir()
    (game_dir / "server").mkdir()
    return game_dir


def prepare_agent_run(
    agent_name: str,
    run_dir: Path,
    server_url: str,
    game_alias: str,
    parsed_run_config: dict[str, object],
    initial_files_dir: Path | None = None,
) -> LaunchPreparation:
    agent_directory(agent_name)
    module = importlib.import_module(f"src.agents.{agent_name}.prepare_run")
    preparation = module.prepare_run(
        run_dir=run_dir,
        server_url=server_url,
        game_alias=game_alias,
        parsed_run_config=parsed_run_config,
        initial_files_dir=initial_files_dir,
    )
    if not isinstance(preparation, LaunchPreparation):
        raise RuntimeError(f"{agent_name}.prepare_run must return LaunchPreparation")
    return preparation


def agent_launch_args(preparation: LaunchPreparation) -> list[str]:
    arguments = []
    for mount in preparation.mounts:
        mode = "ro" if mount.read_only else "rw"
        arguments.extend(["-v", f"{mount.source}:{mount.target}:{mode}"])
    for name, value in preparation.environment.items():
        arguments.extend(["-e", f"{name}={value}"])
    return arguments


def docker_name(prefix: str, name: str) -> str:
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name).strip("-")
    if not safe_name:
        safe_name = "container"
    return f"{prefix}-{os.getpid()}-{safe_name}"


def build_game_id_mapping(games: list[str]) -> dict[str, str]:
    return {uuid4().hex: game for game in games}


def build_container_names(games: list[str]) -> ContainerNames:
    return ContainerNames(
        internal_network=docker_name("env-internal-network", "run"),
        internet_network=docker_name("env-internet-network", "run"),
        proxy=docker_name("env-proxy", "run"),
        servers={game: docker_name("env-server", str(index)) for index, game in enumerate(games, start=1)},
        agents={game: docker_name("env-agent", str(index)) for index, game in enumerate(games, start=1)},
    )


def container_exists(container_name: str) -> bool:
    result = subprocess.run(
        ["docker", "container", "inspect", container_name],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def verify_container_names_available(container_names: ContainerNames) -> None:
    all_names = [container_names.proxy, *container_names.servers.values(), *container_names.agents.values()]
    existing_names = [name for name in all_names if container_exists(name)]
    if existing_names:
        names = " ".join(existing_names)
        raise RuntimeError(f"docker container name already exists: {names}")


def network_exists(network_name: str) -> bool:
    result = subprocess.run(
        ["docker", "network", "inspect", network_name],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def verify_network_name_available(network_name: str) -> None:
    if network_exists(network_name):
        raise RuntimeError(f"docker network name already exists: {network_name}")


def verify_network_names_available(container_names: ContainerNames) -> None:
    verify_network_name_available(container_names.internal_network)
    verify_network_name_available(container_names.internet_network)


def image_exists(image: str) -> bool:
    result = subprocess.run(
        ["docker", "image", "ls", "--quiet", "--filter", f"reference={image}"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"could not query Docker image {image}: {result.stderr.strip()}")
    return bool(result.stdout.strip())


def remove_run_images(images: RunImages) -> None:
    """Remove only this run's tags, preserving other runs and the build cache."""
    for image in asdict(images).values():
        try:
            if not image_exists(image):  # A partial build may not have created it.
                continue
            log_info("removing run image: %s", image)
            result = subprocess.run(
                ["docker", "image", "rm", "--no-prune", image], cwd=ROOT, check=False,
            )
            if result.returncode != 0:
                LOGGER.warning("could not remove run image=%s exit_code=%s", image, result.returncode)
        except (OSError, RuntimeError):
            LOGGER.exception("could not remove run image=%s", image)


@contextmanager
def built_run_images(environment_name: str, agent_name: str) -> Iterator[RunImages]:
    """Own three unique tags across building, account checks and scheduling."""
    images = RunImages.create()
    for image in asdict(images).values():
        if image_exists(image):
            raise RuntimeError(f"run image tag already exists: {image}")
    # Record names before building so partial builds and forced shutdowns can be inspected.
    with (RUN_DIR / "docker_images.json").open("x", encoding="utf-8") as output:
        json.dump(asdict(images), output, indent=2)
        output.write("\n")

    def interrupt_startup(signum: int, _frame: object) -> None:
        LOGGER.warning("received signal %s; cleaning up run images", signum)
        raise KeyboardInterrupt

    with ExitStack() as cleanup:
        # The scheduler temporarily installs its own graceful-stop handlers.
        # These also cover SIGTERM during builds and account preflight checks.
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handler = signal.signal(signum, interrupt_startup)
            cleanup.callback(signal.signal, signum, previous_handler)
        try:
            build_dockers(environment_name, agent_name, images)
            yield images
        finally:
            # A signal arriving during cleanup must not skip the remaining tags.
            # ExitStack restores the caller's handlers after cleanup finishes.
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, signal.SIG_IGN)
            remove_run_images(images)


def build_dockers(environment_name: str, agent_name: str, images: RunImages) -> None:
    environment_directory(environment_name)
    agent_directory(agent_name)
    if not BUILD_DOCKERS_SCRIPT.is_file():
        raise RuntimeError(f"docker build script does not exist: {BUILD_DOCKERS_SCRIPT}")

    command = ["bash", str(BUILD_DOCKERS_SCRIPT), environment_name, agent_name, images.agent, images.server, images.proxy]
    log_info("building docker images for environment %s and agent %s with %s",
             environment_name, agent_name, BUILD_DOCKERS_SCRIPT)
    LOGGER.info("run image tags=%s", asdict(images))
    process = subprocess.Popen(command, cwd=ROOT, start_new_session=True)
    try:
        return_code = process.wait()
    except BaseException:
        # Stop Bash AND its Docker build child before image cleanup can run.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        raise
    if return_code != 0:
        LOGGER.error("docker build failed exit_code=%s", return_code)
        raise RuntimeError(f"docker build failed with exit code {return_code}")
    LOGGER.info("docker build completed")
    for role, image in asdict(images).items():
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        if result.returncode != 0 or not re.fullmatch(r"sha256:[0-9a-f]{64}", result.stdout.strip()):
            raise RuntimeError(f"could not inspect built {role} image: {image}")
        LOGGER.info("built %s image tag=%s id=%s", role, image, result.stdout.strip())


def create_network(network_name: str, *, internal: bool = False) -> None:
    internal_label = " internal" if internal else ""
    log_info("creating%s docker network: %s", internal_label, network_name)
    command = ["docker", "network", "create"]
    if internal:
        command.append("--internal")
    command.append(network_name)
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
    )
    if result.returncode != 0:
        LOGGER.error("docker network create failed network=%s exit_code=%s", network_name, result.returncode)
        raise RuntimeError(f"docker network create failed for {network_name} with exit code {result.returncode}")


def remove_network(network_name: str) -> None:
    if not network_exists(network_name):
        return

    log_info("removing docker network: %s", network_name)
    result = subprocess.run(
        ["docker", "network", "rm", network_name],
        cwd=ROOT,
        check=False,
    )
    if result.returncode != 0:
        LOGGER.error("docker network rm failed network=%s exit_code=%s", network_name, result.returncode)
        log_info("docker network removal failed for %s with exit code %s", network_name, result.returncode)


def docker_proxy_command(
    container_name: str, internet_network: str, internal_network: str, *, image: str,
) -> list[str]:
    return [
        "docker", "run", "--rm", "--pull=never",
        "--name", container_name,
        "--network", internet_network,
        "--network", internal_network,
        image,
    ]


def start_proxy(
    container_name: str, internet_network: str, internal_network: str, *, image: str,
) -> RunningContainer:
    command = docker_proxy_command(container_name, internet_network, internal_network, image=image)
    log_info("starting proxy; container: %s", container_name)
    LOGGER.info("proxy command: %s", command)
    return RunningContainer(container_name, subprocess.Popen(command, cwd=ROOT))


def proxy_env_args(proxy_container_name: str, server_container_name: str) -> list[str]:
    proxy_url = f"http://{proxy_container_name}:{PROXY_PORT}"
    no_proxy = f"{server_container_name},{server_container_name}:{SERVER_PORT},localhost,127.0.0.1,::1"
    return [
        "-e",
        f"HTTP_PROXY={proxy_url}",
        "-e",
        f"HTTPS_PROXY={proxy_url}",
        "-e",
        f"ALL_PROXY={proxy_url}",
        "-e",
        f"http_proxy={proxy_url}",
        "-e",
        f"https_proxy={proxy_url}",
        "-e",
        f"all_proxy={proxy_url}",
        "-e",
        f"NO_PROXY={no_proxy}",
        "-e",
        f"no_proxy={no_proxy}",
    ]


def docker_server_command(
    container_name: str,
    internet_network: str,
    internal_network: str,
    environment: EnvironmentConfig,
    game_alias: str,
    game_id: str,
    *,
    image: str,
) -> list[str]:
    command = [
        "docker", "run", "--rm", "--pull=never",
        "--name", container_name,
        "--network", internet_network,
        "--network", internal_network,
        "-e", "ENV_SERVER_HOST=0.0.0.0",
        "-e", f"ENV_SERVER_PORT={SERVER_PORT}",
        "-v", f"{RUN_DIR / game_alias / 'server'}:/data",
    ]

    # Docker reads values from the host environment; commands and logs contain names only.
    for name in environment.variables:
        command.extend(["-e", name])

    for mount in environment.mounts:
        command.extend(["--mount", f"type=bind,src={mount.source},dst={mount.target}"])

    parameters = dict(environment.parameters)
    if environment.name == "terminal_bench":
        parameters["session_id"] = game_alias

    command.extend([
        image,
        "python", "server/server.py",
        "--environment", environment.name,
        "--game-id", game_id,
        "--game-alias", game_alias,
        "--parameters", json.dumps(parameters, allow_nan=False),
    ])
    return command


def start_server(
    container_name: str,
    internet_network: str,
    internal_network: str,
    environment: EnvironmentConfig,
    game_alias: str,
    game_id: str,
    *,
    image: str,
) -> RunningContainer:
    verify_environment_variables(environment)
    command = docker_server_command(
        container_name, internet_network, internal_network, environment, game_alias, game_id, image=image,
    )
    log_info(
        "starting %s server for game %s as alias %s; container: %s",
        environment.name, game_id, game_alias, container_name,
    )
    LOGGER.info("server command: %s", command)
    return RunningContainer(container_name, subprocess.Popen(command, cwd=ROOT))


def health_check_ok(server: RunningContainer) -> bool:
    # Only probe during startup: the single-threaded server may be busy during play.
    try:
        result = subprocess.run(
            [
                "docker", "exec", server.container_name, "python", "-c",
                (
                    "import sys, urllib.request; "
                    f"response = urllib.request.urlopen('http://127.0.0.1:{SERVER_PORT}/health', timeout=0.5); "
                    "sys.exit(0 if 200 <= response.status < 300 else 1)"
                ),
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2.0,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def stop_container(
    container_name: str,
    process: subprocess.Popen[bytes],
    name: str,
    timeout: int = 20,
) -> None:
    # A signal can arrive just after Popen, before Docker creates the container.
    # Wait briefly here so cleanup cannot miss that in-flight launch.
    deadline = time.monotonic() + 10.0
    while process.poll() is None and not container_exists(container_name):
        if time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    if process.poll() is not None:
        return

    log_info("stopping %s", name)
    stop_result = subprocess.run(
        ["docker", "stop", "--timeout", str(timeout), container_name],
        cwd=ROOT,
        check=False,
    )
    if stop_result.returncode != 0 and process.poll() is None:
        LOGGER.error("docker stop failed for %s exit_code=%s; killing container", name, stop_result.returncode)
        subprocess.run(["docker", "kill", container_name], cwd=ROOT, check=False)

    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        LOGGER.error("killing docker client for %s after timeout", name)
        log_info("killing docker client for %s after timeout", name)
        process.kill()
        process.wait()


def stop_game(game: RunningGame) -> None:
    # Always stop the server, even if stopping its agent fails.
    with ExitStack() as cleanup:
        cleanup.callback(
            stop_container, game.server.container_name, game.server.process, f"server for game {game.game}",
        )
        if game.agent is not None:
            stop_container(game.agent.container_name, game.agent.process, f"agent for game {game.game}")


def stop_all(running: dict[str, RunningGame], proxy: RunningContainer | None) -> None:
    with ExitStack() as cleanup:
        if proxy is not None:
            cleanup.callback(stop_container, proxy.container_name, proxy.process, "proxy")
        for game in reversed(list(running.values())):
            cleanup.callback(stop_game, game)
        running.clear()


def run_scheduler(
    environment: EnvironmentConfig,
    container_names: ContainerNames,
    game_id_mapping: dict[str, str],
    *,
    images: RunImages,
    slots: Sequence[str],
    launch_agent: Callable[[str, str, str], RunningContainer],
) -> int:
    """Run one agent/server pair per slot; a game failure releases only its slot.

    The launcher supplies launch_agent(slot, alias, real_game). Each game is
    attempted once. The exit code is nonzero if any pair fails.
    """
    slots = tuple(slots)
    if not slots or len(set(slots)) != len(slots):
        raise ValueError("Worker slots must be non-empty and unique")

    pending_games = deque(game_id_mapping.items())
    running: dict[str, RunningGame] = {}
    proxy: RunningContainer | None = None
    stopping = False
    failed = False

    def handle_signal(signum: int, _frame: object) -> None:
        nonlocal stopping
        log_info("received signal %s; stopping containers", signum)
        stopping = True

    with ExitStack() as cleanup:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handler = signal.signal(signum, handle_signal)
            cleanup.callback(signal.signal, signum, previous_handler)

        create_network(container_names.internal_network, internal=True)
        cleanup.callback(remove_network, container_names.internal_network)
        create_network(container_names.internet_network)
        cleanup.callback(remove_network, container_names.internet_network)
        cleanup.callback(lambda: stop_all(running, proxy))

        if stopping:
            return 130
        proxy = start_proxy(
            container_names.proxy, container_names.internet_network, container_names.internal_network,
            image=images.proxy,
        )

        while not stopping:
            proxy_return_code = proxy.process.poll()
            if proxy_return_code is not None:
                log_info("proxy stopped with exit code %s; stopping all games", proxy_return_code)
                return proxy_return_code or 1

            for slot in slots:
                if stopping:
                    break
                if slot in running or not pending_games:
                    continue
                alias, game = pending_games.popleft()
                server_name = container_names.servers[game]
                try:
                    prepare_game_directory(alias)
                    server = start_server(
                        server_name, container_names.internet_network, container_names.internal_network,
                        environment, alias, game, image=images.server,
                    )
                    running[slot] = RunningGame(
                        game, alias, server, time.monotonic() + SERVER_START_TIMEOUT_SECONDS,
                    )
                except Exception:
                    LOGGER.exception("could not start server for game %s as alias %s", game, alias)
                    log_info("could not start server for game %s as alias %s; see runner.log", game, alias)
                    failed = True

            for slot, game in list(running.items()):
                if stopping:
                    break
                server_code = game.server.process.poll()
                if server_code is not None:
                    log_info("server for game %s stopped unexpectedly with exit code %s", game.game, server_code)
                    failed = True
                elif game.agent is None:
                    if time.monotonic() >= game.startup_deadline:
                        log_info("server startup timed out for game %s", game.game)
                        failed = True
                    elif health_check_ok(game.server):
                        if stopping:
                            break
                        try:
                            game.agent = launch_agent(slot, game.alias, game.game)
                        except Exception:
                            LOGGER.exception("could not start agent for game %s", game.game)
                            log_info("could not start agent for game %s; see runner.log", game.game)
                            failed = True
                        else:
                            continue
                    else:
                        continue
                else:
                    agent_code = game.agent.process.poll()
                    if agent_code is None:
                        continue
                    log_info("game %s in slot %s stopped with exit code %s", game.game, slot, agent_code)
                    failed |= agent_code != 0

                stop_game(game)
                del running[slot]

            if stopping:
                break
            if not pending_games and not running:
                log_info("all games finished")
                return int(failed)
            time.sleep(1.0)

        return 130
