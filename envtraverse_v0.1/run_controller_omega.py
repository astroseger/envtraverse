#!/usr/bin/env python3
"""Run environment games across Omega ChatGPT accounts mounted in place."""
from __future__ import annotations

import argparse
import signal
import subprocess
import sys
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import run_funs as rf
import run_funs_omega as of


@dataclass(frozen=True)
class RunConfig:
    accounts: list[str]
    games: list[str]
    environment: rf.EnvironmentConfig
    agent: str
    parsed_run_config: dict[str, object]
    runner: of.RunnerConfig = of.RunnerConfig()
    agent_docker_build_args: dict[str, str] = field(default_factory=dict)
    paths: of.RunPaths = of.RunPaths()
    server_inactivity_timeout_seconds: float | None = None
    server_terminal_grace_seconds: float | None = None


def parse_run_config(path: Path, *, paths: of.RunPaths = of.RunPaths()) -> RunConfig:
    loaded_config = rf.load_run_config(path)
    runner = of.parse_runner_config(loaded_config, path)
    inactivity = rf.parse_optional_seconds(loaded_config, path, "server_inactivity_timeout_seconds")
    terminal_grace = rf.parse_optional_seconds(loaded_config, path, "server_terminal_grace_seconds")
    environment = rf.parse_environment_config(loaded_config, path, paths=paths)
    accounts = rf.parse_string_list(loaded_config, "omega_accounts", path)
    games = rf.parse_string_list(loaded_config, "games", path)
    agent = rf.parse_agent_name(loaded_config, path)
    if agent != "omega_agent_v1":
        raise RuntimeError(f"agent in {path} must be omega_agent_v1 for the Omega controller")
    for key in ("model", "reasoning_effort"):
        value = loaded_config.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"{key} in {path} must be a non-empty string")
    build_args = rf.parse_agent_docker_build_args(loaded_config, path)
    rf.validate_unique_games(games)
    if len(set(accounts)) != len(accounts):
        raise RuntimeError("omega_accounts must be unique")
    directories: set[Path] = set()
    for account in accounts:
        directory = of.validate_account(account, paths=paths)
        if directory in directories:
            raise RuntimeError("omega_accounts must refer to distinct account directories")
        directories.add(directory)
    return RunConfig(
        accounts=accounts, games=games, environment=environment, agent=agent,
        parsed_run_config=loaded_config, runner=runner,
        agent_docker_build_args=build_args, paths=paths,
        server_inactivity_timeout_seconds=inactivity,
        server_terminal_grace_seconds=terminal_grace,
    )


def _cleanup_account_check(container_name: str, process: subprocess.Popen[bytes]) -> None:
    # Startup signal handlers raise KeyboardInterrupt. Once cleanup begins, a
    # second signal must not leave a check container holding the live auth mount.
    with ExitStack() as cleanup:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous = signal.signal(signum, signal.SIG_IGN)
            cleanup.callback(signal.signal, signum, previous)
        try:
            if process.poll() is None:
                rf.stop_container(container_name, process, "Omega account check", timeout=5)
        finally:
            # Also handle the race where the Docker client has exited but its
            # newly created container has not yet been removed by --rm.
            try:
                subprocess.run(
                    ["docker", "rm", "-f", container_name], cwd=rf.ROOT,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=False, timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired):
                rf.LOGGER.exception("could not remove Omega account check container=%s", container_name)


def verify_omega_accounts(config: RunConfig, *, agent_image: str) -> None:
    """Check each account using this run's image and its original auth directory."""
    checks_dir = config.paths.run_dir / "account_checks"
    checks_dir.mkdir(parents=True, exist_ok=True)
    for account in config.accounts:
        # The account validator enforces safe single-component account names.
        log_path = checks_dir / f"{account}.log"
        container_name = rf.docker_name("omega-account-check", uuid4().hex)
        command = [
            "docker", "run", "--rm", "--pull=never", "--init", "--name", container_name,
            *of.agent_memory_args(config.runner),
            *of.account_mount_args(account, paths=config.paths),
            agent_image, "python3", "/PeTTa/repos/Omega/scripts/chatgpt-auth",
            "check", "--model", str(config.parsed_run_config["model"]), "--timeout", "120",
        ]
        rf.log_info("checking Omega account %s; log: %s", account, log_path)
        rf.LOGGER.info("Omega account check account=%s image=%s command=%s", account, agent_image, command)
        process: subprocess.Popen[bytes] | None = None
        try:
            with log_path.open("xb") as output:
                process = subprocess.Popen(
                    command, cwd=rf.ROOT, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT,
                )
                try:
                    # The check has an internal 120-second deadline; retain a
                    # host deadline as well for stalled container startup.
                    return_code = process.wait(timeout=150)
                except subprocess.TimeoutExpired:
                    raise RuntimeError(f"Omega account check timed out for {account}; see {log_path}") from None
        finally:
            if process is not None:
                _cleanup_account_check(container_name, process)
        if return_code != 0:
            raise RuntimeError(
                f"Omega account check failed for {account} with exit code {return_code}; see {log_path}"
            )
        rf.LOGGER.info("Omega account check passed account=%s log=%s", account, log_path)


def docker_agent_command(
    account: str,
    container_name: str,
    network_name: str,
    server_container_name: str,
    proxy_container_name: str,
    preparation: rf.LaunchPreparation,
    *,
    agent_image: str,
    runner: of.RunnerConfig = of.RunnerConfig(),
    paths: of.RunPaths = of.RunPaths(),
) -> list[str]:
    return [
        "docker", "run", "--rm", "--pull=never", "--init", "--name", container_name,
        *of.agent_memory_args(runner),
        *of.account_mount_args(account, paths=paths),
        "--network", network_name,
        *rf.proxy_env_args(proxy_container_name, server_container_name),
        *rf.agent_launch_args(preparation),
        agent_image, *preparation.command,
    ]


def start_agent(
    account: str,
    game_alias: str,
    real_game: str,
    container_name: str,
    network_name: str,
    server_container_name: str,
    proxy_container_name: str,
    preparation: rf.LaunchPreparation,
    *,
    agent_image: str,
    runner: of.RunnerConfig = of.RunnerConfig(),
    paths: of.RunPaths = of.RunPaths(),
) -> rf.RunningContainer:
    command = docker_agent_command(
        account, container_name, network_name, server_container_name, proxy_container_name,
        preparation, agent_image=agent_image, runner=runner, paths=paths,
    )
    log_path = paths.run_dir / game_alias / "run" / "omega_console.log"
    rf.log_info(
        "starting game %s as alias %s on Omega account %s; container: %s; log: %s",
        real_game, game_alias, account, container_name, log_path,
    )
    rf.LOGGER.info(
        "agent command real_game=%s game_alias=%s account=%s command=%s",
        real_game, game_alias, account, command,
    )
    # The Docker process retains its own descriptor after this parent closes
    # the file. No reader thread or supervisor is needed to persist console logs.
    with log_path.open("xb") as output:
        process = subprocess.Popen(
            command, cwd=rf.ROOT, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT,
        )
    return rf.RunningContainer(container_name=container_name, process=process)


def run_scheduler(
    config: RunConfig,
    container_names: rf.ContainerNames,
    game_id_mapping: dict[str, str],
    *,
    images: rf.RunImages,
) -> int:
    def launch_agent(account: str, game_alias: str, real_game: str) -> rf.RunningContainer:
        preparation = rf.prepare_agent_run(
            config.agent, config.paths.run_dir / game_alias,
            f"http://{container_names.servers[real_game]}:{rf.SERVER_PORT}",
            game_alias, config.parsed_run_config,
            initial_files_dir=rf.initial_workspace_files(config.environment.name),
        )
        return start_agent(
            account, game_alias, real_game, container_names.agents[real_game],
            container_names.internal_network, container_names.servers[real_game],
            container_names.proxy, preparation, agent_image=images.agent,
            runner=config.runner, paths=config.paths,
        )

    return rf.run_scheduler(
        config.environment, container_names, game_id_mapping, images=images,
        slots=config.accounts, launch_agent=launch_agent, paths=config.paths,
        server_inactivity_timeout_seconds=config.server_inactivity_timeout_seconds,
        server_terminal_grace_seconds=config.server_terminal_grace_seconds,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run games across Omega ChatGPT accounts.")
    of.add_run_path_arguments(parser)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    run_created = False
    try:
        paths = of.RunPaths(root=args.run_root, accounts_root=args.accounts_root)
        run_config = paths.resolve_config(args.run_config)
        config = parse_run_config(run_config, paths=paths)
        rf.verify_environment_variables(config.environment)
        game_id_mapping = rf.build_game_id_mapping(config.games)
        container_names = rf.build_container_names(config.games)
        rf.verify_container_names_available(container_names)
        rf.verify_network_names_available(container_names)
        rf.prepare_run_root(paths)
        run_created = True
        rf.setup_runner_logging(paths)
        rf.LOGGER.info("runner log started: %s", paths.runner_log)
        rf.LOGGER.info("source root=%s run root=%s accounts root=%s", rf.ROOT, paths.root, paths.accounts_dir)
        rf.LOGGER.info(
            "run config=%s accounts=%s games=%s environment=%s environment_parameters=%s environment_variables=%s",
            run_config, config.accounts, config.games, config.environment.name,
            config.environment.parameters, config.environment.variables,
        )
        rf.LOGGER.info("selected agent=%s", config.agent)
        rf.LOGGER.info("runner memory limits=%s", config.runner)
        rf.LOGGER.info("game alias mapping=%s", game_id_mapping)
        rf.LOGGER.info(
            "docker names internal_network=%s internet_network=%s proxy=%s servers=%s agents=%s",
            container_names.internal_network, container_names.internet_network,
            container_names.proxy, container_names.servers, container_names.agents,
        )
        with rf.built_run_images(
            config.environment.name, config.agent,
            agent_docker_build_args=config.agent_docker_build_args, paths=paths,
        ) as images:
            verify_omega_accounts(config, agent_image=images.agent)
            exit_code = run_scheduler(config, container_names, game_id_mapping, images=images)
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


if __name__ == "__main__":
    raise SystemExit(main())
