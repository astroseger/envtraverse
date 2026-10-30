# Running Terminal-Bench experiments

Terminal-Bench needs a host runtime to manage task containers and grading in
addition to the usual agent and environment-server containers. For the task
interface and adapter design, see the
[Terminal-Bench environment](src/server/environments/terminal_bench/README.md).

Run all commands from `envtraverse_v0.1/`. Start with the
[general running guide](docs/running/README.md) to prepare accounts and choose an
agent and model.

## Requirements

Use Linux, Python 3.12+, Git, Docker Engine, and Docker Compose v2. Run the host
runtime as the same user that runs the experiment so the server can access its
socket. Archive any existing `run/` and `run_tb/` directories in the selected
run root before starting a new experiment.

## Run with subscription accounts

Edit the agent, tasks, accounts, model, and effort in
[run_config_terminal_bench.yaml](run_config_terminal_bench.yaml), then run:

```bash
python3 run_controller_tb.py --run-config run_config_terminal_bench.yaml
```

The wrapper installs the host dependencies and selected benchmark data (2.1 by
default), starts the runtime, runs `run_controller.py`, and waits for grading and
cleanup. Runtime output appears with a `[runtime]` prefix and is also saved to
`run_tb/runtime.log`.

The supplied small configuration is:

```yaml
agent: agent_v1
environment: terminal_bench
server_mounts:
  - source: ./run_tb/socket
    target: /run/terminal-bench
games:
  - terminal-bench-2.1-fix-git
  - terminal-bench-2.1-regex-log
codex_accounts: [codex01]
codex_account_fast_service_tier: true
model: gpt-5.6-sol
reasoning_effort: max
```

Keep the socket mount; its source is relative to the selected run root, which
defaults to `envtraverse_v0.1/`, even if the configuration lives elsewhere.
The runtime creates the directory before the
controller starts. The fast service tier is optional and defaults to `false`
when omitted.

Use `--run-root` to keep an experiment outside the source checkout:

```bash
python3 run_controller_tb.py --run-root /path/to/experiment --benchmarks 4.0
```

This reads `/path/to/experiment/run_config.yaml`, uses account data from
`/path/to/experiment/codex_accounts/`, and creates `run/` and `run_tb/` there.
Source code, account helper scripts, the Harbor virtual environment, and downloaded
benchmarks stay in the checkout. An explicit relative `--run-config` path is
resolved against the caller's working directory. See
[run roots](docs/running/README.md#choose-a-run-root) for account setup and path rules.

For the full supplied task list, use:

```bash
python3 run_controller_tb.py --run-config run_config_terminal_bench_2.1.yaml
```

For all 70 tasks in Terminal-Bench-Science 0.1, use:

```bash
python3 run_controller_tb.py --benchmarks science-0.1 --run-config run_config_terminal_bench_science_0.1.yaml
```

The supplied [Science configuration](run_config_terminal_bench_science_0.1.yaml) lists the published
`v0.1.0` tasks using slash-separated IDs. It selects `agent_v1_07_wstop2`, with
`http_read_timeout_seconds: 36300` (10 hours 5 minutes) and
`supervisor_inactivity_timeout_seconds: 39600` (11 hours) to accommodate long
initialization. The Terminal-Bench manager defaults to no socket timeout;
Harbor's native task time budgets still apply. Fast service tier is disabled.

`--benchmarks` accepts `2.1`, `3.0`, `4.0`, `science-0.1`, comma-separated
combinations such as `3.0,4.0`, or `all`, matching `setup.sh`. An explicit selection
must include every benchmark named in the configuration's `games:` list; mismatches
fail before setup. The bundled smoke test is allowed with any selection. The
configuration still selects the tasks to execute: `all` prepares every dataset
and runs the configured games. Omitting the option retains default TB2.1 setup.

Always pass the intended configuration: without `--run-config`, the wrapper uses
`run_config.yaml`. Concurrency follows `codex_accounts`. Grading can overlap later
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

The wrapper uses subscription accounts. For an API-key experiment, first run
`setup.sh` as above. In one terminal, start the runtime:

```bash
./src/server/environments/terminal_bench/host_runtime/run.sh
```

Wait for `Runtime ready` and leave that terminal running. In another terminal:

```bash
export OPENAI_API_KEY='your-openai-api-key'
python3 run_controller_with_api_key.py --run-config run_config_terminal_bench.yaml
```

The API-key runner allows all listed tasks to run concurrently and ignores
`codex_accounts`. Use a task list that fits the host's resources. For manual
subscription runs, use `run_controller.py` in the second terminal instead.

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
