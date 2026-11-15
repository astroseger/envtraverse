# Running Terminal-Bench experiments

Terminal-Bench needs a host runtime to manage task containers and grading in
addition to the usual agent and environment-server containers. For the task
interface and adapter design, see the
[Terminal-Bench environment](src/server/environments/terminal_bench/README.md).

Unless stated otherwise, examples assume you are in `envtraverse_v0.1/`. Start with
the
[general running guide](docs/running/README.md) to prepare accounts and choose an
agent and model.

## Requirements

Use Linux, Python 3.12+, Git, Docker Engine, and Docker Compose v2. Run the host
runtime as the same user that runs the experiment so the server can access its
socket. Archive any existing `run/` and `run_tb/` directories in the selected
run root before starting a new experiment.

## Run with Codex ChatGPT accounts

Start with [run_config_terminal_bench.yaml](run_config_examples/run_config_terminal_bench.yaml).
Copy it to a local `run_config.yaml` and edit the agent, tasks, accounts, model,
and effort before running. This example uses a prepared Codex account at
`codex_accounts/codex01`; change `accounts` to match your account directory names.

```bash
cp run_config_examples/run_config_terminal_bench.yaml run_config.yaml
# Edit run_config.yaml for your experiment.
python3 run_controller_tb.py
```

The checkout's local `run_config.yaml` is ignored by Git. To use a supplied
preset directly, pass its path under `run_config_examples/` with `--run-config`.
That uses the preset's accounts and model settings; it does not merge in your
local `run_config.yaml`.

The wrapper installs the host dependencies and selected benchmark data (2.1 by
default), starts the runtime, runs `run_controller.py`, and waits for grading and
cleanup. Runtime output appears with a `[runtime]` prefix and is also saved to
`run_tb/runtime.log`.

The supplied preset defaults to `backend: codex_chatgpt_accounts` because it
omits `backend`. You can make that choice explicit in your local configuration:

```yaml
backend: codex_chatgpt_accounts
agent: agent_v1
agent_parameters:
  model: gpt-5.6-sol
  reasoning_effort: max
  codex_account_fast_service_tier: true
environment: terminal_bench
environment_parameters:
  environment_version: "v1.2"
server_mounts:
  - source: ./run_tb/socket
    target: /run/terminal-bench
games:
  - terminal-bench-2.1-fix-git
  - terminal-bench-2.1-regex-log
accounts: [codex01]
```

Keep the socket mount; its source is relative to the selected run root, which
defaults to the caller's working directory at startup, even if the configuration
lives elsewhere. The runtime creates the directory before the controller starts.
The fast service tier is optional and defaults to `false` when omitted.

All supplied Terminal-Bench configurations select interface `v1.2`, which uses
`get_path` to copy sandbox files and directories into the agent workspace.
Use `environment_parameters.environment_version: "v1"` for the original
`get_file` observation behavior. Omitting this parameter also selects `v1`;
the default does not follow newer releases. This interface version is independent
of the benchmark dataset version in the game IDs.

### Use OpenCode ChatGPT accounts

Prepare an account with the [OpenCode account tools](opencode_chatgpt_accounts/README.md).
In your local Terminal-Bench configuration, change these fields:

```yaml
backend: opencode_chatgpt_accounts
agent: opencode_agent_v1_08_wstop3
accounts: [account01]
agent_parameters:
  model: openai/gpt-6.1-terra
  reasoning_effort: max
```

Use your actual account names under `opencode_chatgpt_accounts/` and a model
available to those accounts. Keep `environment: terminal_bench`, the task list,
the socket mount, and any timeout overrides. Remove the Codex-only
`codex_account_fast_service_tier` setting and any
`agent_docker_build_args.CODEX_VERSION` entry from the chosen preset.
Run the same `run_controller_tb.py` command. See the
[OpenCode agent guide](src/agents/opencode_agent_v1_08_wstop3/README.md)
for native options and account lifecycle details.

### Keep an experiment outside the checkout

For the Codex 2.1 example above, copy your edited configuration into the experiment
directory and reuse the prepared accounts in the checkout:

```bash
mkdir -p /path/to/experiment
cp run_config.yaml /path/to/experiment/run_config.yaml
python3 run_controller_tb.py \
  --run-root /path/to/experiment \
  --accounts-dir "$PWD/codex_accounts"
```

This reads `/path/to/experiment/run_config.yaml`, uses the original account
directories without copying them, and creates `run/` and `run_tb/` under the
experiment directory. For OpenCode, use `--accounts-dir "$PWD/opencode_chatgpt_accounts"`.
Without `--accounts-dir`, accounts are read from the selected backend's account
folder under the run root, such as `/path/to/experiment/codex_accounts/`.
Source code, account helper scripts, the Harbor virtual environment, and downloaded
benchmarks stay in the checkout. An explicit relative `--run-config` path is
resolved against the caller's working directory and does not change the run
root. You can also start the wrapper by its absolute path from the experiment
directory and omit `--run-root`. See
[run roots](docs/running/README.md#choose-a-run-root) for account setup and path rules.

## Choose tasks and datasets

The configuration's `games` list chooses the tasks to run. `--benchmarks` chooses
which datasets to prepare; it does not add tasks to that list. Omit it for 2.1,
or pass the dataset selection matching your configured tasks.

To switch presets, replace your local configuration and reapply your account,
backend, and model settings before starting another experiment. Archive the
previous `run/` and `run_tb/` first. For the full supplied 2.1 task list:

```bash
cp run_config_examples/run_config_terminal_bench_2.1.yaml run_config.yaml
# Edit run_config.yaml: reapply your backend, agent, accounts, model, and effort.
python3 run_controller_tb.py
```

For all 70 tasks in Terminal-Bench-Science 0.1, use:

```bash
cp run_config_examples/run_config_terminal_bench_science_0.1.yaml run_config.yaml
# Reapply your backend, agent, accounts, and model; retain the long timeouts.
python3 run_controller_tb.py --benchmarks science-0.1
```

The supplied [Science configuration](run_config_examples/run_config_terminal_bench_science_0.1.yaml) lists the published
`v0.1.0` tasks using slash-separated IDs. It selects `agent_v1_08_wstop3`, with
`http_read_timeout_seconds: 36300` (10 hours 5 minutes) and
`supervisor_inactivity_timeout_seconds: 39600` (11 hours) in `agent_parameters` to accommodate long
initialization. The Terminal-Bench manager defaults to no socket timeout;
Harbor's native task time budgets still apply. Fast service tier is disabled.

`--benchmarks` accepts `2.1`, `3.0`, `4.0`, `science-0.1`, comma-separated
combinations such as `3.0,4.0`, or `all`, matching `setup.sh`. An explicit selection
must include every benchmark named in the configuration's `games:` list; mismatches
fail before setup. The bundled smoke test is allowed with any selection. The
configuration still selects the tasks to execute: `all` prepares every dataset
and runs the configured games. Omitting the option retains default TB2.1 setup.

Always pass the intended configuration: without `--run-config`, the wrapper uses
`run_config.yaml`. Concurrency follows `accounts`. Grading can overlap later
tasks, so host task containers may outnumber active agents.

## Find task IDs

To install the data and inspect tasks before running an experiment:

```bash
./src/server/environments/terminal_bench/host_runtime/setup.sh
./src/server/environments/terminal_bench/host_runtime/list_tasks.sh \
  --benchmark terminal-bench-2.1 --format yaml
```

Setup installs the pinned Harbor version in `host_runtime/.venv/` and downloads
Terminal-Bench 2.1 into `host_runtime/.tb-data/`. It reuses the recorded dataset
revision on later runs. These reusable files are separate from experiment
output; task images are prepared when tasks start.

Optional datasets can be installed with `setup.sh --benchmarks science-0.1`,
`setup.sh --benchmarks 3.0,4.0`, or `setup.sh --benchmarks all`.
See the script's `--help` and
[dataset sources](src/server/environments/terminal_bench/host_runtime/UPSTREAM.md)
for revision and location options. The wrapper forwards its `--benchmarks`
selection to setup.

## API-key runs and manual runtime

The configuration's `backend` selects authentication. To run an API-key
experiment, start the runtime manually and launch the controller directly.
The API-key confirmation needs an interactive terminal, which the wrapper's
controller process does not have. First run `setup.sh` with the datasets needed
by your configuration, as above. In one terminal, start the runtime:

```bash
./src/server/environments/terminal_bench/host_runtime/run.sh
```

Wait for `Runtime ready` and leave that terminal running. Set
`backend: codex_openai_api_key` and select a Codex agent, such as
`agent_v1_08_wstop3`, in your local `run_config.yaml`. Then run in another terminal:

```bash
export OPENAI_API_KEY='your-openai-api-key'
python3 run_controller.py
```

Read the cost warning and type `continue` when the controller asks.

Keep agent settings inside `agent_parameters`; the API-key backend requires
this layout. It does not use an `accounts` list or account directories.
The API-key backend runs all listed tasks concurrently by default; add
`max_parallel_games: 2` to cap active games. For manual subscription runs, set
`backend: codex_chatgpt_accounts` and use the same controller command.

For an external experiment, pass the same `--run-root /path/to/experiment` to
both `host_runtime/run.sh` and the controller. The runtime's socket, results,
and agent-workspace lookup then use that root.

After the controller finishes, stop the runtime with Ctrl+C and let it finish
grading and cleanup. The runtime remains running when idle until stopped.

## Finish and keep the output

With the wrapper, Ctrl+C stops the controller and then drains the runtime.
Allow it to finish before archiving results. Task grading happens after solving;
the controller's exit status is not a task score.

Keep both directories under the selected run root:

- `run/`: agent workspaces, observations, client state, and server logs.
- `run_tb/`: host-runtime logs and `results/<alias>/`, including `result.json`
  and the Harbor trial output.

The same generated alias connects a task's files across both directories. Rename
or archive both before starting another experiment. See the
[analysis guide](analysis_scripts/README.md#terminal-bench) for reports from the
completed runs.
