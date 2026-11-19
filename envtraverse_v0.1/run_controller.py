#!/usr/bin/env python3
"""Run experiments with a selected Codex or OpenCode authentication backend."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
from pathlib import Path
import subprocess
import sys
from typing import cast

from src.controller import run_backends as backends
from src.controller import run_funs as rf


LEGACY_AGENT_PARAMETERS = (
    "model", "reasoning_effort", "http_read_timeout_seconds",
    "supervisor_inactivity_timeout_seconds", "recovery_delay_seconds",
    "max_recovery_attempts", "codex_account_fast_service_tier",
)
OPENCODE_AGENT_PARAMETERS = (
    "opencode_thinking", "opencode_agent", "opencode_shell_timeout_background",
)


@dataclass(frozen=True)
class RunConfig:
    games: list[str]
    environment: rf.EnvironmentConfig
    agent: str
    parsed_run_config: dict[str, object]
    backend: str = backends.CODEX_CHATGPT
    accounts: list[str] = field(default_factory=list)
    max_parallel_games: str | int = "all"
    codex_account_fast_service_tier: bool = False
    runner: rf.RunnerConfig = rf.RunnerConfig()
    agent_docker_build_args: dict[str, str] = field(default_factory=dict)
    paths: rf.RunPaths = field(default_factory=rf.RunPaths)
    server_inactivity_timeout_seconds: float | None = None
    server_terminal_grace_seconds: float | None = None

    @property
    def agent_parameters(self) -> dict[str, object]:
        return cast(dict[str, object], self.parsed_run_config["agent_parameters"])


def select_backend(config: dict[str, object]) -> str:
    backend = config.get("backend", backends.CODEX_CHATGPT)
    if not isinstance(backend, str) or backend not in backends.BACKENDS:
        raise RuntimeError("backend must be one of: " + ", ".join(sorted(backends.BACKENDS)))
    return backend


def normalize_run_config(loaded: dict[str, object], *, backend: str, path: Path) -> dict[str, object]:
    """Convert supported legacy Codex settings to the canonical configuration."""
    normalized = dict(loaded)
    parameters = normalized.get("agent_parameters", {})
    if not isinstance(parameters, dict):
        raise RuntimeError(f"agent_parameters in {path} must be a mapping")
    parameters = dict(parameters)
    normalized["agent_parameters"] = parameters
    normalized["backend"] = backend

    for alias in ("codex_accounts", "opencode_accounts"):
        if alias not in normalized:
            continue
        if backend != backends.CODEX_CHATGPT or alias != "codex_accounts":
            raise RuntimeError(f"{alias} in {path} is unsupported for backend {backend}; use accounts")
        if "accounts" in normalized:
            raise RuntimeError(f"duplicate configuration for accounts in {path}: accounts and {alias}")
        normalized["accounts"] = normalized.pop(alias)

    for key in (*LEGACY_AGENT_PARAMETERS, *OPENCODE_AGENT_PARAMETERS):
        if key not in normalized:
            continue
        if backend != backends.CODEX_CHATGPT or key in OPENCODE_AGENT_PARAMETERS:
            raise RuntimeError(f"{key} in {path} must move under agent_parameters.{key}")
        if key in parameters:
            raise RuntimeError(f"duplicate configuration for {key} in {path}: {key} and agent_parameters.{key}")
        parameters[key] = normalized.pop(key)
    return normalized


def parse_accounts(config: dict[str, object], backend: str, path: Path) -> list[str]:
    if backend == backends.CODEX_API_KEY:
        return []
    return rf.parse_string_list(config, "accounts", path)


def parse_run_config(path: Path, *, paths: rf.RunPaths | None = None) -> RunConfig:
    paths = paths or rf.RunPaths()
    loaded = rf.load_run_config(path)
    backend = select_backend(loaded)
    loaded = normalize_run_config(loaded, backend=backend, path=path)
    paths = replace(paths, backend=backend)
    if backend != backends.CODEX_API_KEY and "max_parallel_games" in loaded:
        raise RuntimeError("max_parallel_games is only valid for backend codex_openai_api_key")
    parallelism = loaded.get("max_parallel_games", "all")
    if parallelism != "all" and (type(parallelism) is not int or parallelism <= 0):
        raise RuntimeError("max_parallel_games must be 'all' or a positive integer")
    parameters = cast(dict[str, object], loaded["agent_parameters"])
    fast_tier = parameters.get("codex_account_fast_service_tier", False)
    if not isinstance(fast_tier, bool):
        raise RuntimeError(f"agent_parameters.codex_account_fast_service_tier in {path} must be a boolean")
    games = rf.parse_string_list(loaded, "games", path)
    rf.validate_unique_games(games)
    config = RunConfig(
        games=games,
        environment=rf.parse_environment_config(loaded, path, paths=paths),
        agent=rf.parse_agent_name(loaded, path),
        parsed_run_config=loaded,
        backend=backend,
        accounts=parse_accounts(loaded, backend, path),
        max_parallel_games=parallelism,
        codex_account_fast_service_tier=fast_tier,
        runner=rf.parse_runner_config(loaded, path),
        agent_docker_build_args=rf.parse_agent_docker_build_args(loaded, path),
        paths=paths,
        server_inactivity_timeout_seconds=rf.parse_optional_seconds(loaded, path, "server_inactivity_timeout_seconds"),
        server_terminal_grace_seconds=rf.parse_optional_seconds(loaded, path, "server_terminal_grace_seconds"),
    )
    backends.create_backend(config).validate()
    return config


def docker_agent_command(slot: str, container_name: str, network_name: str,
                         server_container_name: str, proxy_container_name: str,
                         preparation: rf.LaunchPreparation, *, agent_image: str,
                         backend: backends.RunBackend) -> list[str]:
    return [
        "docker", "run", "--rm", "--pull=never", "--name", container_name,
        *rf.agent_memory_args(backend.config.runner),
        *backend.authentication_args(slot),
        "--network", network_name,
        *rf.proxy_env_args(proxy_container_name, server_container_name),
        *rf.agent_launch_args(preparation), agent_image, *preparation.command,
    ]


def start_agent(slot: str, game_alias: str, real_game: str, container_name: str,
                network_name: str, server_container_name: str, proxy_container_name: str,
                preparation: rf.LaunchPreparation, *, agent_image: str,
                backend: backends.RunBackend, check_cancelled) -> rf.RunningContainer:
    check_cancelled()
    backend.prepare_game(slot, game_alias, real_game, container_name, agent_image)
    check_cancelled()
    command = docker_agent_command(slot, container_name, network_name, server_container_name,
                                   proxy_container_name, preparation, agent_image=agent_image, backend=backend)
    rf.log_info("starting game %s as alias %s with backend %s in slot %s; container: %s",
                real_game, game_alias, backend.config.backend, slot, container_name)
    if backend.log_command:
        rf.LOGGER.info("agent command game=%s alias=%s slot=%s command=%s", real_game, game_alias, slot, command)
    else:
        rf.LOGGER.info("agent command game=%s alias=%s uses CODEX_API_KEY; command not logged", real_game, game_alias)
    check_cancelled()
    process = subprocess.Popen(command, cwd=rf.ROOT, stdin=subprocess.DEVNULL,
                               env=backend.process_environment())
    return rf.RunningContainer(container_name, process)


def run_scheduler(config: RunConfig, container_names: rf.ContainerNames,
                  game_id_mapping: dict[str, str], *, images: rf.RunImages,
                  backend: backends.RunBackend) -> int:
    def launch_agent(slot: str, game_alias: str, real_game: str, check_cancelled) -> rf.RunningContainer:
        check_cancelled()
        preparation = rf.prepare_agent_run(
            config.agent, config.paths.run_dir / game_alias,
            f"http://{container_names.servers[real_game]}:{rf.SERVER_PORT}", game_alias,
            config.agent_parameters, initial_files_dir=rf.initial_workspace_files(config.environment.name),
        )
        check_cancelled()
        return start_agent(
            slot, game_alias, real_game, container_names.agents[real_game],
            container_names.internal_network, container_names.servers[real_game], container_names.proxy,
            preparation, agent_image=images.agent, backend=backend, check_cancelled=check_cancelled,
        )

    return rf.run_scheduler(
        config.environment, container_names, game_id_mapping, images=images,
        slots=backend.slots(len(game_id_mapping)), launch_agent=launch_agent,
        finalize_game=lambda slot, game: backend.finalize_game(slot, game, images.agent), paths=config.paths,
        server_inactivity_timeout_seconds=config.server_inactivity_timeout_seconds,
        server_terminal_grace_seconds=config.server_terminal_grace_seconds,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run games with Codex ChatGPT, OpenCode ChatGPT, or Codex API-key authentication.")
    rf.add_run_path_arguments(parser)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    paths = rf.RunPaths(args.run_root, args.accounts_dir)
    run_created = False
    try:
        run_config = paths.resolve_config(args.run_config)
        config = parse_run_config(run_config, paths=paths)
        paths = config.paths
        backend = backends.create_backend(config)
        backend.authorize()
        rf.verify_environment_variables(config.environment)
        game_id_mapping = rf.build_game_id_mapping(config.games)
        container_names = rf.build_container_names(config.games)
        rf.verify_container_names_available(container_names)
        rf.verify_network_names_available(container_names)
        with backend.reserve():
            rf.prepare_run_root(paths)
            run_created = True
            rf.setup_runner_logging(paths)
            rf.LOGGER.info("runner log started: %s", paths.runner_log)
            rf.LOGGER.info("source root=%s run root=%s run config=%s", rf.ROOT, paths.root, run_config)
            rf.LOGGER.info("backend=%s accounts=%s max_parallel_games=%s agent=%s games=%s",
                           config.backend, config.accounts, config.max_parallel_games, config.agent, config.games)
            rf.LOGGER.info("environment=%s parameters=%s variables=%s", config.environment.name,
                           config.environment.parameters, config.environment.variables)
            rf.LOGGER.info("runner memory limits=%s", config.runner)
            rf.LOGGER.info("game alias mapping=%s", game_id_mapping)
            rf.LOGGER.info("docker names=%s", container_names)
            with rf.built_run_images(config.environment.name, config.agent,
                                     agent_docker_build_args=config.agent_docker_build_args, paths=paths) as images:
                backend.preflight(images.agent)
                exit_code = run_scheduler(config, container_names, game_id_mapping, images=images, backend=backend)
        rf.LOGGER.info("runner finished exit_code=%s", exit_code)
        return exit_code
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        if rf.LOGGER.handlers:
            rf.LOGGER.exception("runner failed")
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if run_created:
            rf.make_run_writable(paths)


# Compatibility aliases for callers that imported the old Codex helper names.
CHECK_CODEX_ACCOUNT_SCRIPT = backends.CHECK_CODEX_ACCOUNT_SCRIPT
CLEAN_CODEX_ACCOUNT_SCRIPT = backends.CLEAN_CODEX_ACCOUNT_SCRIPT
clean_codex_account = backends.clean_codex_account
verify_codex_accounts = backends.verify_codex_accounts


if __name__ == "__main__":
    raise SystemExit(main())
