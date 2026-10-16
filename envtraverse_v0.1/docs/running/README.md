# Running experiments

This guide covers the supplied Codex agents and runners. For the framework's
interaction model and deliberate isolation policy, see the
[framework README](../../README.md).

**Run all commands below from `envtraverse_v0.1/`.**

## Requirements

- Linux with Docker available to your user.
- Python 3.11+ and PyYAML on the host.
- A Codex subscription account or an OpenAI API key for the bundled agents.
- Any credentials required by the environment, such as `ARC_API_KEY`.
- For Terminal-Bench: Python 3.12+, Git, and Docker Compose v2.

The runners build the agent, server, and proxy images automatically, including
the selected environment's Python dependencies.

## First run with a subscription account

Install PyYAML and prepare an account directory:

```bash
python3 -m pip install PyYAML
./codex_accounts/prepare_account.sh codex_accounts/codex01
./codex_accounts/check_account.sh codex_accounts/codex01
```

Follow the device-authentication instructions. The scripts build their account
tools image on first use; see [account tools](../../codex_accounts/README.md).

The supplied [run_config.yaml](../../run_config.yaml) selects the baseline agent
and the counter example. A complete configuration with an explicit target is:

```yaml
agent: agent_v1
environment: example
environment_parameters:
  target: 3
games: [counter]
codex_accounts: [codex01]
model: gpt-5.6-sol
reasoning_effort: max
```

Set `model` and `reasoning_effort` to the configuration you want to study; both
are required by the bundled agents. Start the run:

```bash
python3 run_controller.py --run-config run_config.yaml
```

Results go to `run/` inside the framework directory. Rename or archive an existing
`run/` before starting another experiment. Passing a different configuration
path does not change this output location. For long experiments, use `tmux` or
`screen`.

## Choose an agent

Change the `agent` field in your configuration:

| Agent | `agent` value | Intended environments |
| --- | --- | --- |
| Baseline | `agent_v1` | All bundled environments |
| Stop-and-report | `agent_v1_01_wstop` | All bundled environments |
| Controlled_RESET | `test_arc3_agent_v1_02_wstop_wreset` | ARC-AGI-3 |

For example, set `agent: agent_v1_01_wstop` to run Stop-and-report with the same
tasks. Compare agents in separate experiments with the same environment settings.
See [Agents](../../src/agents/README.md) for the implementations and integration
instructions.

## Choose an environment

`environment` selects an adapter. The `games` list contains its task IDs, including
for tasks that are not games. Optional `environment_parameters` configure that
adapter; `environment_variables` lists host variable names to forward to its
server. Both can be omitted when unused.

### ARC-AGI-3

Edit the agent, game IDs, model, and effort in
[run_config_arc3.yaml](../../run_config_arc3.yaml), then run:

```bash
export ARC_API_KEY='your-arc-api-key'
python3 run_controller.py --run-config run_config_arc3.yaml
```

The supplied configuration forwards `ARC_API_KEY` to the server and uses
`competition: false`. For competition runs, set
`environment_parameters.competition: true`. See the
[ARC-AGI-3 adapter](../../src/server/environments/arc3/README.md) for its settings.

### AGI Maze: individual labyrinths

Edit [run_config_agimaze.yaml](../../run_config_agimaze.yaml) and run:

```bash
python3 run_controller.py --run-config run_config_agimaze.yaml
```

Task IDs are paths relative to the bundled
[maze packs](../../src/server/environments/agimaze_single/labyrinth/packs/), for
example `TUTORIAL/STAGE-01/0000.json`. No environment API key is needed.

### AGI Maze: labyrinth sets

For a sequence of tutorials followed by a target maze, save the following as
`run_config_maze_set.yaml`:

```yaml
agent: agent_v1_01_wstop
environment: agimaze_set_v1
games: [ts0_CLASSIC_EASY_pits_4x4]
codex_accounts: [codex01]
model: gpt-5.6-sol
reasoning_effort: max
```

```bash
python3 run_controller.py --run-config run_config_maze_set.yaml
```

The agent keeps its workspace and conversation across the mazes in a set.
Choose other sequences from the
[set definitions](../../src/server/environments/agimaze_set_v1/sets/); task IDs
are filenames without `.json`. No environment API key is needed.

### Terminal-Bench

Terminal-Bench needs a host runtime in addition to the agent and server containers.
After preparing the accounts and configuration, run:

```bash
python3 run_controller_tb.py --run-config run_config_terminal_bench.yaml
```

The wrapper sets up the host runtime, starts the subscription controller, and
waits for grading and cleanup. The supplied
[small configuration](../../run_config_terminal_bench.yaml) has two tasks;
[run_config_terminal_bench_2.1.yaml](../../run_config_terminal_bench_2.1.yaml)
contains the full supplied task list.

Keep the `server_mounts` socket entry in these configurations. Archive both
`run/` and `run_tb/` before another experiment. See the
[Terminal-Bench running guide](../../README_tb.md) for setup details, task
discovery, and API-key runs.

## Run more tasks concurrently

The subscription runner uses one worker per entry in `codex_accounts` and queues
remaining tasks. Prepare another account directory:

```bash
./codex_accounts/prepare_account.sh codex_accounts/codex02
```

Then set `codex_accounts: [codex01, codex02]`. Each directory is authenticated
separately; directories using the same subscription share its usage limits.
Use separate directories for concurrent experiments because the runner clears
saved Codex state before each task while retaining authentication.

The optional `codex_account_fast_service_tier: true` enables the fast service
tier for subscription runs. Its default is `false`; the supplied Terminal-Bench
configurations enable it.

## Run with an API key

Use the same agent and environment configuration:

```bash
export OPENAI_API_KEY='your-openai-api-key'
python3 run_controller_with_api_key.py --run-config run_config.yaml
```

The runner checks Codex with the API key and asks for `continue` before launching.
It ignores `codex_accounts` and allows **all listed tasks to run concurrently**;
there is currently no configurable concurrency cap. Choose the task list for the
host's available resources. Each agent container has a 10 GiB memory limit;
environment servers and Terminal-Bench task containers use additional resources.

`OPENAI_API_KEY` authenticates the agent. Keep environment credentials in the
environment's own `environment_variables` configuration. For Terminal-Bench,
follow the [manual runtime instructions](../../README_tb.md#api-key-runs-and-manual-runtime)
before starting this runner.

## Find and keep the results

With the bundled agents, each task receives a generated alias and fresh files:

| Path | Contents |
| --- | --- |
| `run/runner.log` | Task mapping, scheduling, and container launch log |
| `run/<alias>/run/` | Agent code, logs, and writable `agent_run/` workspace |
| `run/<alias>/client/` | Client configuration and saved session state |
| `run/<alias>/observations/` | Environment description, actions, and observation files |
| `run/<alias>/server/` | Server logs and environment outputs |
| `run_tb/` | Additional Terminal-Bench host-runtime results |

Ctrl+C requests cleanup of active containers and networking. For Terminal-Bench,
allow grading and cleanup to finish before archiving results. A controller exit
code reports execution status; task outcomes are recorded by the environment.

See the [analysis guide](../../analysis_scripts/README.md) for reports, replay,
token extraction, and run diagnostics.
