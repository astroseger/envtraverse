#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import run_funs as rf


rf.set_logger_name("arc-runner.api-key")
CHECK_CODEX_API_KEY_SCRIPT = rf.CODEX_ACCOUNTS_DIR / "check_codex_with_api_key.sh"
API_KEY_WARNING = """\
Running with API keys is very expensive. The used version of codex have a picularity that with API key the percentage of input token wich hit the cache is very low (in my test run which cost me 300$ it was <20%), when you run it via ChatGPT PRO subsciption this percentage is much higher >95%.

Because of this what you can run with a weekly limit of a single ChatGPT PRO account, would probably cost you > 10000$ with API key.

So think twice before your run it.

If you are sure. Type continue
"""


@dataclass(frozen=True)
class RunConfig:
    games: list[str]
    environment: rf.EnvironmentConfig
    agent: str
    parsed_run_config: dict[str, object]


def parse_run_config(path: Path) -> RunConfig:
    loaded_config = rf.load_run_config(path)
    environment = rf.parse_environment_config(loaded_config, path)
    games = rf.parse_string_list(loaded_config, "games", path)
    agent = rf.parse_agent_name(loaded_config, path)
    rf.validate_unique_games(games)
    return RunConfig(
        games=games,
        environment=environment,
        agent=agent,
        parsed_run_config=loaded_config,
    )


def verify_codex_api_key() -> None:
    if not CHECK_CODEX_API_KEY_SCRIPT.is_file():
        raise RuntimeError(f"codex API key check script does not exist: {CHECK_CODEX_API_KEY_SCRIPT}")
    if not os.access(CHECK_CODEX_API_KEY_SCRIPT, os.X_OK):
        raise RuntimeError(f"codex API key check script is not executable: {CHECK_CODEX_API_KEY_SCRIPT}")

    rf.LOGGER.info("checking codex API key with %s", CHECK_CODEX_API_KEY_SCRIPT)
    result = subprocess.run([str(CHECK_CODEX_API_KEY_SCRIPT)], cwd=rf.ROOT, check=False)
    if result.returncode != 0:
        rf.LOGGER.error("codex API key check failed exit_code=%s", result.returncode)
        raise RuntimeError(f"codex API key check failed with exit code {result.returncode}")
    rf.LOGGER.info("codex API key check passed")


def docker_agent_command(
    container_name: str,
    network_name: str,
    server_container_name: str,
    proxy_container_name: str,
    preparation: rf.LaunchPreparation,
    *,
    agent_image: str,
) -> list[str]:
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
        "-e",
        f"CODEX_API_KEY={os.environ['OPENAI_API_KEY']}",
        "--network",
        network_name,
        *rf.proxy_env_args(proxy_container_name, server_container_name),
        *rf.agent_launch_args(preparation),
        agent_image,
        *preparation.command,
    ]


def start_agent(
    game_alias: str,
    real_game: str,
    container_name: str,
    network_name: str,
    server_container_name: str,
    proxy_container_name: str,
    preparation: rf.LaunchPreparation,
    *,
    agent_image: str,
) -> rf.RunningContainer:
    command = docker_agent_command(
        container_name,
        network_name,
        server_container_name,
        proxy_container_name,
        preparation,
        agent_image=agent_image,
    )
    rf.log_info(
        "starting game %s as alias %s with API key; container: %s",
        real_game,
        game_alias,
        container_name,
    )
    rf.LOGGER.info(
        "agent command real_game=%s game_alias=%s container=%s contains CODEX_API_KEY; command not logged",
        real_game,
        game_alias,
        container_name,
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
    def launch_agent(_slot: str, game_alias: str, real_game: str) -> rf.RunningContainer:
        preparation = rf.prepare_agent_run(
            config.agent,
            rf.RUN_DIR / game_alias,
            f"http://{container_names.servers[real_game]}:{rf.SERVER_PORT}",
            game_alias,
            config.parsed_run_config,
            initial_files_dir=rf.initial_workspace_files(config.environment.name),
        )
        return start_agent(
            game_alias,
            real_game,
            container_names.agents[real_game],
            container_names.internal_network,
            container_names.servers[real_game],
            container_names.proxy,
            preparation,
            agent_image=images.agent,
        )

    return rf.run_scheduler(
        config.environment,
        container_names,
        game_id_mapping,
        images=images,
        slots=list(game_id_mapping),  # One slot per game: launch all games in parallel.
        launch_agent=launch_agent,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run games in parallel with CODEX_API_KEY from OPENAI_API_KEY.")
    parser.add_argument(
        "--run-config",
        type=Path,
        default=rf.RUN_CONFIG,
        help=f"run config to read; default: {rf.RUN_CONFIG}",
    )
    return parser.parse_args(argv)


def confirm_api_key_run() -> None:
    print(API_KEY_WARNING, flush=True)
    if input("> ") != "continue":
        raise RuntimeError("aborted")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    run_created = False
    try:
        confirm_api_key_run()
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set in the environment")
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
            "run config=%s games=%s environment=%s environment_parameters=%s environment_variables=%s",
            run_config,
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
            verify_codex_api_key()
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
