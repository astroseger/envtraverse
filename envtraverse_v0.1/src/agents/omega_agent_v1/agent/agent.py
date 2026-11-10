"""Start one environment task, then replace this process with unchanged Omega.

There is no continuation loop or recovery supervisor here. Omega receives the
task once through the local channel and uses its own autonomous loop afterward.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
DEFAULT_AGENT_RUN_DIR = ROOT / "agent_run"
WORKSPACE_INIT_DIR = ROOT / "workspace_init"
PROMPT_DIR = ROOT / "prompts"
CLIENT_SCRIPT_PATH = Path("/client/client.py")
CLIENT_STATE_PATH = Path("/client/.client_session.json")
OBSERVATIONS_DIR = Path("/observations")
ENVIRONMENT_DESCRIPTION_PATH = OBSERVATIONS_DIR / "environment_description.md"
PETTA_ROOT = Path("/PeTTa")
OMEGA_ROOT = PETTA_ROOT / "repos/Omega"
MEMORY_TEMPLATE_DIR = Path("/opt/omega-memory-template")


def positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def ensure_fresh_run_paths(run_dir: Path) -> None:
    existing = [path for path in (
        run_dir, CLIENT_STATE_PATH, ROOT / "agent.log", ROOT / "task_prompt.txt",
        ROOT / "omega-policy.json", ROOT / "omega-logging.conf",
    ) if path.exists() or path.is_symlink()]
    if existing:
        raise FileExistsError("Fresh Omega run requires these paths to be absent: "
                              + ", ".join(map(str, existing)))
    if not CLIENT_SCRIPT_PATH.is_file() or not OBSERVATIONS_DIR.is_dir():
        raise FileNotFoundError("Fresh Omega run requires the /client and /observations mounts")
    if any(OBSERVATIONS_DIR.iterdir()):
        raise FileExistsError(f"Fresh Omega run requires empty observations: {OBSERVATIONS_DIR}")
    for path in (PETTA_ROOT / "run.sh", OMEGA_ROOT / "lib_omega.metta",
                 MEMORY_TEMPLATE_DIR, ROOT / "run_task.metta", ROOT / "prompt_channel.py"):
        if not path.exists():
            raise FileNotFoundError(f"Missing Omega runtime resource: {path}")


def prepare_agent_run(run_dir: Path) -> None:
    if WORKSPACE_INIT_DIR.is_dir():
        # Omega's unchanged imports and history helper expect ./repos/Omega.
        # Reserve that path instead of replacing a task's existing files.
        if (WORKSPACE_INIT_DIR / "repos").exists() or (WORKSPACE_INIT_DIR / "repos").is_symlink():
            raise FileExistsError("workspace_init/repos is reserved for the Omega runtime")
        shutil.copytree(WORKSPACE_INIT_DIR, run_dir)
    else:
        run_dir.mkdir()
    (run_dir / "repos").symlink_to(PETTA_ROOT / "repos", target_is_directory=True)


def initialize_omega_memory() -> None:
    """Seed an empty, per-run memory mount with the image's stock prompt files."""
    destination = OMEGA_ROOT / "memory"
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise FileExistsError(
            f"Fresh Omega run requires an empty per-run memory mount at {destination}")
    shutil.copytree(MEMORY_TEMPLATE_DIR, destination, dirs_exist_ok=True)
    (destination / "chroma_db").mkdir(exist_ok=True)


def build_initial_prompt(initial_client_output: str) -> str:
    main_prompt = (PROMPT_DIR / "main_prompt.md").read_text(encoding="utf-8").strip()
    continuation = (PROMPT_DIR / "continuation_string.txt").read_text(encoding="utf-8").strip()
    if not main_prompt:
        raise ValueError("main_prompt.md must not be empty")
    return (main_prompt + "\n\n" + continuation
            + "\n\nThe initial output of the game client:\n" + initial_client_output)


def write_runtime_files(initial_prompt: str) -> tuple[Path, Path, Path]:
    prompt_path = ROOT / "task_prompt.txt"
    prompt_path.write_text(initial_prompt, encoding="utf-8")
    auth_file = Path(os.environ.get("OMEGA_CHATGPT_AUTH_FILE", "~/.omega_auth/chatgpt.json")).expanduser()
    cache = Path.home() / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    # /client and /observations need write access for the unchanged client's
    # session updates and observation storage. The task prompt forbids direct
    # edits to observations, matching agent_v1's existing convention.
    policy = {
        "version": 1,
        "filesystem_policy": {
            "include_workdir": False,
            "read_only": ["/bin", "/usr", "/lib", "/proc", "/dev/urandom", "/etc",
                          "/opt", "/var/log", "/sys/devices/system", str(PETTA_ROOT)],
            "read_write": [str(ROOT), str(OMEGA_ROOT / "memory"), "/client", "/observations",
                           str(auth_file.parent), str(cache), "/tmp", "/var/tmp", "/dev/shm",
                           "/dev/null", "/opt/huggingface", "/opt/sentence_transformers"],
        },
        "landlock": {"compatibility": "best_effort"},
    }
    policy_path = ROOT / "omega-policy.json"
    # JSON is accepted by Omega's YAML policy reader; keep launcher dependencies
    # in the standard library.
    policy_path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
    logging_path = ROOT / "omega-logging.conf"
    logging_path.write_text(
        "[loggers]\nkeys=root,httpx,httpcore\n\n"
        "[handlers]\nkeys=console,file\n\n"
        "[formatters]\nkeys=default\n\n"
        "[logger_root]\nlevel=INFO\nhandlers=console,file\n\n"
        "[logger_httpx]\nlevel=WARNING\nhandlers=\nqualname=httpx\npropagate=1\n\n"
        "[logger_httpcore]\nlevel=WARNING\nhandlers=\nqualname=httpcore\npropagate=1\n\n"
        "[handler_console]\nclass=StreamHandler\nlevel=NOTSET\nformatter=default\n"
        "args=(sys.stderr,)\n\n"
        "[handler_file]\nclass=FileHandler\nlevel=NOTSET\nformatter=default\n"
        f"args=({str(ROOT / 'agent.log')!r}, 'a', 'utf-8')\n\n"
        "[formatter_default]\nformat=%(asctime)s | %(levelname)-8s | %(name)s | %(message)s\n"
        "datefmt=%Y-%m-%d %H:%M:%S\n",
        encoding="utf-8",
    )
    return prompt_path, policy_path, logging_path


def build_omega_command(args, prompt_path: Path, policy_path: Path, logging_path: Path) -> list[str]:
    return [
        "/bin/sh", str(PETTA_ROOT / "run.sh"), str(ROOT / "run_task.metta"),
        "provider=ChatGPT", "embeddingprovider=Local", "commchannel=envtraverse",
        f"model={args.model}", f"reasoningMode={args.reasoning_effort}",
        f"maxNewInputLoops={args.max_new_input_loops}", "maxWakeLoops=0",
        f"memoryDirectory={OMEGA_ROOT / 'memory'}",
        f"chromaDbPath={OMEGA_ROOT / 'memory/chroma_db'}",
        f"taskPromptPath={prompt_path}", f"securityPolicyPath={policy_path}",
        f"logConfigPath={logging_path}",
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send one task prompt to Omega's autonomous loop.")
    parser.add_argument("--master", required=True, metavar="GAME_NAME")
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--max-new-input-loops", type=positive_integer, default=10_000_000)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        ensure_fresh_run_paths(DEFAULT_AGENT_RUN_DIR)
        prepare_agent_run(DEFAULT_AGENT_RUN_DIR)
        initialize_omega_memory()
        initial = subprocess.run(
            [sys.executable, str(CLIENT_SCRIPT_PATH), "start", args.master],
            cwd=DEFAULT_AGENT_RUN_DIR, text=True, capture_output=True, check=True,
        )
        if initial.stderr:
            print(initial.stderr, end="", file=sys.stderr)
        if not ENVIRONMENT_DESCRIPTION_PATH.is_file() or not ENVIRONMENT_DESCRIPTION_PATH.read_text(encoding="utf-8").strip():
            raise RuntimeError("Client did not save the server's environment description")
        paths = write_runtime_files(build_initial_prompt(initial.stdout))
        command = build_omega_command(args, *paths)
        environment = dict(os.environ)
        environment["CHROMA_DB_PATH"] = str(OMEGA_ROOT / "memory/chroma_db")
        # Python-backed built-in skills execute while lib_omega is imported,
        # before the local channel starts. Make all installed Omega modules
        # importable from the beginning of embedded-Python initialization.
        module_paths = [OMEGA_ROOT / "src", OMEGA_ROOT / "providers",
                        OMEGA_ROOT / "profile", OMEGA_ROOT,
                        PETTA_ROOT / "repos/petta_lib_chromadb"]
        python_paths = list(map(str, module_paths))
        if environment.get("PYTHONPATH"):
            python_paths.append(environment["PYTHONPATH"])
        environment["PYTHONPATH"] = os.pathsep.join(python_paths)
        # This process becomes PeTTa/Omega. There is no runner, restart loop,
        # terminal-state monitor, or subsequent prompt submission.
        print(f"Starting Omega with one task prompt and {args.max_new_input_loops} active cycles.", flush=True)
        os.chdir(DEFAULT_AGENT_RUN_DIR)
        os.execvpe(command[0], command, environment)
    except subprocess.CalledProcessError as error:
        print(f"Environment initialization failed (exit {error.returncode}).", file=sys.stderr)
        if error.stderr:
            print(error.stderr, end="", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Omega startup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
