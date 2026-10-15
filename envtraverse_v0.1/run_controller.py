#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import run_funs as rf


CHECK_CODEX_ACCOUNT_SCRIPT = rf.CODEX_ACCOUNTS_DIR / "check_account.sh"
CLEAN_CODEX_ACCOUNT_SCRIPT = rf.CODEX_ACCOUNTS_DIR / "clean_account.sh"


@dataclass(frozen=True)
class RunConfig:
    accounts: list[str]
    games: list[str]
    environment: rf.EnvironmentConfig
    agent: str
    parsed_run_config: dict[str, object]
    codex_account_fast_service_tier: bool = False


def parse_run_config(path: Path) -> RunConfig:
    loaded_config = rf.load_run_config(path)
    environment = rf.parse_environment_config(loaded_config, path)
    accounts = rf.parse_string_list(loaded_config, "codex_accounts", path)
    games = rf.parse_string_list(loaded_config, "games", path)
    agent = rf.parse_agent_name(loaded_config, path)
    fast_service_tier = loaded_config.get("codex_account_fast_service_tier", False)
    if not isinstance(fast_service_tier, bool):
        raise RuntimeError(f"codex_account_fast_service_tier in {path} must be a boolean")
    rf.validate_unique_games(games)

    for account in accounts:
        account_dir = rf.CODEX_ACCOUNTS_DIR / account
        if not account_dir.is_dir():
            raise RuntimeError(f"codex account folder does not exist: {account_dir}")

    return RunConfig(
        accounts=accounts,
        games=games,
        environment=environment,
        agent=agent,
        parsed_run_config=loaded_config,
        codex_account_fast_service_tier=fast_service_tier,
    )


def clean_codex_account(account: str, *, fast_service_tier: bool = False) -> None:
    if not CLEAN_CODEX_ACCOUNT_SCRIPT.is_file():
        raise RuntimeError(f"codex account clean script does not exist: {CLEAN_CODEX_ACCOUNT_SCRIPT}")
    if not os.access(CLEAN_CODEX_ACCOUNT_SCRIPT, os.X_OK):
        raise RuntimeError(f"codex account clean script is not executable: {CLEAN_CODEX_ACCOUNT_SCRIPT}")

    account_path = rf.CODEX_ACCOUNTS_DIR / account
    rf.LOGGER.info("cleaning codex account account=%s path=%s", account, account_path)
    result = subprocess.run(
        [str(CLEAN_CODEX_ACCOUNT_SCRIPT), str(account_path)],
        cwd=rf.ROOT,
        check=False,
    )
    if result.returncode != 0:
        rf.LOGGER.error("codex account clean failed account=%s exit_code=%s", account, result.returncode)
        raise RuntimeError(
            f"codex account clean failed for {account} with exit code {result.returncode}"
        )
    if fast_service_tier:
        with (account_path / "config.toml").open("a", encoding="utf-8") as config_file:
            config_file.write('service_tier = "fast"\n')
    rf.LOGGER.info("codex account clean completed account=%s fast_service_tier=%s", account, fast_service_tier)


def verify_codex_accounts(config: RunConfig) -> None:
    if not CHECK_CODEX_ACCOUNT_SCRIPT.is_file():
        raise RuntimeError(f"codex account check script does not exist: {CHECK_CODEX_ACCOUNT_SCRIPT}")
    if not os.access(CHECK_CODEX_ACCOUNT_SCRIPT, os.X_OK):
        raise RuntimeError(f"codex account check script is not executable: {CHECK_CODEX_ACCOUNT_SCRIPT}")

    for account in config.accounts:
        account_path = rf.CODEX_ACCOUNTS_DIR / account
        rf.LOGGER.info("checking codex account account=%s path=%s", account, account_path)
        result = subprocess.run(
            [str(CHECK_CODEX_ACCOUNT_SCRIPT), str(account_path)],
            cwd=rf.ROOT,
            check=False,
        )
        if result.returncode != 0:
            rf.LOGGER.error("codex account check failed account=%s exit_code=%s", account, result.returncode)
            raise RuntimeError(
                f"codex account check failed for {account} with exit code {result.returncode}"
            )
        rf.LOGGER.info("codex account check passed account=%s", account)


def docker_agent_command(
    account: str,
    container_name: str,
    network_name: str,
    server_container_name: str,
    proxy_container_name: str,
    preparation: rf.LaunchPreparation,
    *,
    agent_image: str,
) -> list[str]:
    account_dir = rf.CODEX_ACCOUNTS_DIR / account
    return [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--name",
        container_name,
        "--memory",
        rf.AGENT_MEMORY_LIMIT,
        "--memory-swap",
        rf.AGENT_MEMORY_LIMIT,
        "--ulimit",
        f"data={rf.AGENT_PROCESS_DATA_LIMIT_BYTES}:{rf.AGENT_PROCESS_DATA_LIMIT_BYTES}",
        "-v",
        f"{account_dir}:/home/user/.codex",
        "--network",
        network_name,
        *rf.proxy_env_args(proxy_container_name, server_container_name),
        *rf.agent_launch_args(preparation),
        agent_image,
        *preparation.command,
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
    fast_service_tier: bool = False,
) -> rf.RunningContainer:
    clean_codex_account(account, fast_service_tier=fast_service_tier)
    command = docker_agent_command(
        account,
        container_name,
        network_name,
        server_container_name,
        proxy_container_name,
        preparation,
        agent_image=agent_image,
    )
    rf.log_info(
        "starting game %s as alias %s on codex account %s; container: %s",
        real_game,
        game_alias,
        account,
        container_name,
    )
    rf.LOGGER.info(
        "agent command real_game=%s game_alias=%s account=%s command=%s",
        real_game,
        game_alias,
        account,
        command,
    )
    process = subprocess.Popen(
        command,
        cwd=rf.ROOT,
        stdin=subprocess.DEVNULL,
    )
    return rf.RunningContainer(
        container_name=container_name,
        process=process,
    )


def run_scheduler(
    config: RunConfig,
    container_names: rf.ContainerNames,
    game_id_mapping: dict[str, str],
    *,
    images: rf.RunImages,
) -> int:
    def launch_agent(account: str, game_alias: str, real_game: str) -> rf.RunningContainer:
        preparation = rf.prepare_agent_run(
            config.agent,
            rf.RUN_DIR / game_alias,
            f"http://{container_names.servers[real_game]}:{rf.SERVER_PORT}",
            game_alias,
            config.parsed_run_config,
            initial_files_dir=rf.initial_workspace_files(config.environment.name),
        )
        return start_agent(
            account,
            game_alias,
            real_game,
            container_names.agents[real_game],
            container_names.internal_network,
            container_names.servers[real_game],
            container_names.proxy,
            preparation,
            agent_image=images.agent,
            fast_service_tier=config.codex_account_fast_service_tier,
        )

    return rf.run_scheduler(
        config.environment,
        container_names,
        game_id_mapping,
        images=images,
        slots=config.accounts,
        launch_agent=launch_agent,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run games across codex accounts.")
    parser.add_argument(
        "--run-config",
        type=Path,
        default=rf.RUN_CONFIG,
        help=f"run config to read; default: {rf.RUN_CONFIG}",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    run_created = False
    try:
        run_config = args.run_config.resolve()
        config = parse_run_config(run_config)
        rf.verify_environment_variables(config.environment)
        game_id_mapping = rf.build_game_id_mapping(config.games)
        container_names = rf.build_container_names(config.games)
        rf.verify_container_names_available(container_names)
        rf.verify_network_names_available(container_names)
        rf.prepare_run_root()
        run_created = True
        rf.setup_runner_logging()
        rf.LOGGER.info("runner log started: %s", rf.RUNNER_LOG_PATH)
        rf.LOGGER.info(
            "run config=%s accounts=%s games=%s environment=%s environment_parameters=%s environment_variables=%s",
            run_config,
            config.accounts,
            config.games,
            config.environment.name,
            config.environment.parameters,
            config.environment.variables,
        )
        rf.LOGGER.info("selected agent=%s", config.agent)
        rf.LOGGER.info("game alias mapping=%s", game_id_mapping)
        rf.LOGGER.info(
            "docker names internal_network=%s internet_network=%s proxy=%s servers=%s agents=%s",
            container_names.internal_network,
            container_names.internet_network,
            container_names.proxy,
            container_names.servers,
            container_names.agents,
        )
        with rf.built_run_images(config.environment.name, config.agent) as images:
            verify_codex_accounts(config)
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
            rf.make_run_writable()


if __name__ == "__main__":
    raise SystemExit(main())
