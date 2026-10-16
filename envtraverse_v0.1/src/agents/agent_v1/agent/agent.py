from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sys
import time
from pathlib import Path

from agent_funs import (
    load_prompt,
    run_git_snapshot,
    run_program,
    send_prompt,
    setup_logger,
)
from codex_runner import CodexRunner
from session_inspector import (
    CLIENT_SCRIPT_PATH,
    CLIENT_STATE_PATH,
    ENVIRONMENT_DESCRIPTION_PATH,
    OBSERVATIONS_DIR,
    SessionInspection,
    inspect_sessions,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_AGENT_RUN_DIR = ROOT / "agent_run"
WORKSPACE_INIT_DIR = ROOT / "workspace_init"
PROMPT_DIR = ROOT / "prompts"
LOG_FILE = ROOT / "agent.log"
THREAD_ID_FILE = ROOT / ".codex_thread_id"
THREAD_ID_TEMP_FILE = ROOT / ".codex_thread_id.tmp"


def resolve_log_file() -> Path:
    return LOG_FILE


def normalize_thread_id(thread_id: str) -> str:
    normalized_thread_id = thread_id.strip()
    if not normalized_thread_id or any(character.isspace() for character in normalized_thread_id):
        raise ValueError("Codex thread ID must be a nonempty value without whitespace")
    return normalized_thread_id


def save_thread_id(thread_id: str) -> None:
    normalized_thread_id = normalize_thread_id(thread_id)
    THREAD_ID_TEMP_FILE.write_text(normalized_thread_id + "\n", encoding="utf-8")
    THREAD_ID_TEMP_FILE.replace(THREAD_ID_FILE)


def load_thread_id() -> str:
    return normalize_thread_id(THREAD_ID_FILE.read_text(encoding="utf-8"))


def ensure_fresh_run_paths(run_dir: Path) -> None:
    existing_paths = [
        path
        for path in (
            run_dir,
            CLIENT_STATE_PATH,
            LOG_FILE,
            LOG_FILE.with_name("codex_stderr.log"),
            THREAD_ID_FILE,
            THREAD_ID_TEMP_FILE,
        )
        if path.exists()
    ]
    if existing_paths:
        existing_list = ", ".join(str(path) for path in existing_paths)
        raise FileExistsError(f"Fresh master run requires these paths to be absent: {existing_list}")
    if not CLIENT_SCRIPT_PATH.is_file() or not OBSERVATIONS_DIR.is_dir():
        raise FileNotFoundError("Fresh master run requires the /client and /observations mounts")
    if any(OBSERVATIONS_DIR.iterdir()):
        raise FileExistsError(f"Fresh master run requires empty observations: {OBSERVATIONS_DIR}")


def prepare_agent_run(run_dir: Path) -> None:
    """Copy an optional workspace template, or start with an empty workspace."""
    if WORKSPACE_INIT_DIR.is_dir():
        shutil.copytree(WORKSPACE_INIT_DIR, run_dir)
    else:
        run_dir.mkdir()


def ensure_recovery_run_paths(run_dir: Path) -> None:
    missing_paths = [
        path
        for path in (run_dir, CLIENT_SCRIPT_PATH, CLIENT_STATE_PATH, OBSERVATIONS_DIR)
        if not path.exists()
    ]
    if missing_paths:
        missing_list = ", ".join(str(path) for path in missing_paths)
        raise FileNotFoundError(f"Recovery requires existing run paths: {missing_list}")


class SimpleAgent:
    def __init__(
        self,
        run_dir: str | Path,
        game_name: str | None = None,
        model: str = "gpt-5.5",
        reasoning_effort: str = "medium",
    ):
        self.run_dir = Path(run_dir)
        self.log_file = resolve_log_file()
        self.game_name = game_name
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.logger = setup_logger(self.log_file)
        self.logger.info(
            "agent parameters",
            extra={
                "run_dir": str(self.run_dir.resolve()),
                "game_name": self.game_name,
                "model": self.model,
                "reasoning_effort": self.reasoning_effort,
            },
        )
        self.runner = CodexRunner(
            work_dir=self.run_dir,
            log_file=self.log_file,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            on_thread_started=save_thread_id,
        )
        self.iteration_id = 0
        self.previous_loop_step_index: int | None = None
        self.last_stuck_step: int | None = None

        self.main_prompt = load_prompt(PROMPT_DIR, "main_prompt.md", self.logger)
        self.continuation_string = load_prompt(
            PROMPT_DIR,
            "continuation_string.txt",
            self.logger,
        )
        self.normal_continuation_prompt = load_prompt(
            PROMPT_DIR,
            "continuation.txt",
            self.logger,
        )
        self.stuck_reminder_prompt = load_prompt(
            PROMPT_DIR,
            "stuck_reminder_prompt.txt",
            self.logger,
        )

    def validate_environment_description(self) -> None:
        # The client saves the server-provided description when it starts the game.
        description_path = ENVIRONMENT_DESCRIPTION_PATH
        if not description_path.is_file() or not description_path.read_text(encoding="utf-8").strip():
            raise RuntimeError("CRITICAL: client did not save the server's environment description")
        self.logger.info("environment description validated")

    def init_iterations(self) -> None:
        if self.game_name is None:
            raise RuntimeError("game_name is required for master mode initialization.")

        game_init_screenout = self.run_client(f"start {self.game_name}")
        self.start_codex_session(game_init_screenout)

    def start_codex_session(self, game_init_screenout: str) -> None:
        self.validate_environment_description()
        self.runner.new_session()
        initial_prompt = (
            self.main_prompt
            + "\n\n"
            + self.continuation_string
            + "\n\nThe initial output of the game client:\n"
            + game_init_screenout
        )
        self.send_prompt(initial_prompt)
        self.logger.info("saved Codex thread ID", extra={"path": str(THREAD_ID_FILE)})

    def init_recovery(self) -> None:
        game_init_screenout = self.run_client("status")
        inspection = inspect_sessions()
        self.logger.info(
            "start init_recovery",
            extra={**inspection.to_dict(), "client_status": game_init_screenout},
        )
        if inspection.terminal:
            return
        if inspection.step_index == 0:
            self.logger.info("recovering step 0 with a new Codex thread")
            self.start_codex_session(game_init_screenout)
        else:
            self.runner.resume_session(load_thread_id())
            self.normal_continuation_protocol()

    def run_git(self, inspection: SessionInspection) -> None:
        run_git_snapshot(
            agent_run_dir=self.run_dir,
            level_index=None,
            global_step_count=inspection.step_index,
            iteration_count=self.iteration_id,
            logger=self.logger,
        )

    def run_client(self, command: str) -> str:
        return run_program(
            f"python3 {shlex.quote(str(CLIENT_SCRIPT_PATH))} {command}",
            cwd=self.run_dir,
            logger=self.logger,
        )

    def send_prompt(self, prompt: str) -> None:
        send_prompt(self.runner, prompt, self.logger)

    def stop_condition(self, inspection: SessionInspection) -> bool:
        if inspection.terminal:
            self.logger.info("stop condition met", extra={"reason": "environment is terminal"})
            return True
        if self.last_stuck_step is not None and inspection.step_index == self.last_stuck_step:
            self.logger.info(
                "stop condition met",
                extra={"reason": "no progress after stuck protocol"},
            )
            return True
        return False

    def continuation_prompt(self) -> str:
        return self.normal_continuation_prompt + "\n" + self.continuation_string

    def normal_continuation_protocol(self) -> None:
        self.logger.info("selected protocol", extra={"protocol": "normal_continuation_protocol"})
        self.send_prompt(self.continuation_prompt())

    def stuck_protocol(self) -> None:
        self.logger.info("selected protocol", extra={"protocol": "stuck_protocol"})
        self.send_prompt(self.continuation_prompt() + "\n" + self.stuck_reminder_prompt)

    def iteration_loop(self, inspection: SessionInspection) -> None:
        self.run_git(inspection)

        if self.previous_loop_step_index == inspection.step_index:
            self.stuck_protocol()
            self.last_stuck_step = inspection.step_index
        else:
            self.normal_continuation_protocol()

        self.previous_loop_step_index = inspection.step_index

    def run(self, recovery: bool = False) -> int:
        self.logger.info(
            "starting agent",
            extra={
                "game_name": self.game_name,
                "run_dir": str(self.run_dir.resolve()),
                "recovery": recovery,
                "model": self.model,
                "reasoning_effort": self.reasoning_effort,
            },
        )

        try:
            if recovery:
                self.init_recovery()
            else:
                self.init_iterations()
        except Exception:
            self.logger.exception("initialization failed")
            return 1

        while True:
            self.iteration_id += 1
            try:
                inspection = inspect_sessions()
            except Exception:
                self.logger.exception(
                    "failed to inspect sessions",
                    extra={"iteration_id": self.iteration_id},
                )
                return 1

            self.logger.info(
                "iteration inspection",
                extra={
                    "iteration_id": self.iteration_id,
                    **inspection.to_dict(),
                },
            )

            if self.stop_condition(inspection):
                self.logger.info("stopping main loop", extra={"iteration_id": self.iteration_id})
                return 0

            try:
                self.iteration_loop(inspection)
            except Exception:
                self.logger.exception(
                    "iteration loop failed",
                    extra={"iteration_id": self.iteration_id},
                )
                return 1

            time.sleep(1.0)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the simple agent.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--master", metavar="GAME_NAME")
    group.add_argument("--recovery", action="store_true")
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--reasoning-effort", default="medium")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if args.master is not None:
        run_dir = DEFAULT_AGENT_RUN_DIR
        ensure_fresh_run_paths(run_dir)
        prepare_agent_run(run_dir)
        agent = SimpleAgent(
            run_dir=run_dir,
            game_name=args.master,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
        )
        return agent.run()

    if args.recovery:
        run_dir = DEFAULT_AGENT_RUN_DIR
        ensure_recovery_run_paths(run_dir)
        agent = SimpleAgent(
            run_dir=run_dir,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
        )
        return agent.run(recovery=True)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
