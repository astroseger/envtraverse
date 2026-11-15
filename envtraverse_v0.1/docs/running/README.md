# Running experiments

This guide covers the supplied Codex and OpenCode agents and shared controller. For the framework's
interaction model and deliberate isolation policy, see the
[framework README](../../README.md).

**Run all commands below from `envtraverse_v0.1/`.**

## Requirements

- Linux with Docker available to your user.
- Python 3.11+ and PyYAML on the host.
- A ChatGPT account for Codex or OpenCode, or an OpenAI API key for Codex.
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

The supplied [run_config.yaml](../../run_config_examples/run_config.yaml) selects the baseline agent
and the counter example. Tracked presets live in `run_config_examples/`; make a
local copy for your experiment:

```bash
cp run_config_examples/run_config.yaml run_config.yaml
```

The checkout's local `run_config.yaml` is ignored by Git. A complete configuration
with an explicit target is:

```yaml
backend: codex_chatgpt_accounts
agent: agent_v1
agent_parameters:
  model: gpt-5.6-sol
  reasoning_effort: max
environment: example
environment_parameters:
  target: 3
games: [counter]
accounts: [codex01]
```

Set `model` and `reasoning_effort` inside `agent_parameters` to the configuration
you want to study; both are required by the bundled agents. Start the run:

```bash
python3 run_controller.py --run-config run_config.yaml
```

Results go to `<run-root>/run/`. The run root defaults to the caller's working
directory at startup, which is the checkout for the commands above.
Rename or archive an existing `run/` in the selected root before another
experiment. Passing a different configuration path alone does not change this
output location. For long experiments, use `tmux` or `screen`.

## Choose a backend

`backend` selects authentication and account handling; `agent` independently
selects the compatible agent implementation.

| Backend | Credentials | Concurrency |
| --- | --- | --- |
| `codex_chatgpt_accounts` | `accounts` under `codex_accounts/` | One game per account |
| `opencode_chatgpt_accounts` | `accounts` under `opencode_chatgpt_accounts/` | One game per account |
| `codex_openai_api_key` | Host `OPENAI_API_KEY` | `max_parallel_games: all` or a positive integer |

The API-key concurrency limit defaults to `all`. Any `max_parallel_games` key
on a ChatGPT backend is an error, even if its value is `all`. Model and reasoning
settings retain the selected agent's behavior; OpenCode requires both explicitly.

For OpenCode, prepare an account with the
[OpenCode account tools](../../opencode_chatgpt_accounts/README.md), then run:

```bash
python3 run_controller.py --run-config run_config_examples/run_config_opencode.yaml
```

The supplied config selects `opencode_agent_v1_08_wstop3`. Its account is mounted
in place; current credentials are retained when history is cleaned between games.

Configurations without `backend` default to Codex ChatGPT. The controller uses
`run_config.yaml` by default; select another configuration with `--run-config`.

## Agent parameters and legacy configurations

Put runtime agent settings under `agent_parameters`: model and reasoning effort,
client and supervisor timeouts, recovery settings, OpenCode options, and
`codex_account_fast_service_tier`. The selected agent/backend validates its own
settings; custom agents may use additional keys and structured values. Omitted
settings retain the agent's existing defaults. `agent_docker_build_args` and
the `runner` resource limits remain separate top-level settings.

For `codex_chatgpt_accounts` only, the loader accepts these legacy locations and
converts them once before passing configuration to the controller and hooks:

| Legacy root key | Normalized location |
| --- | --- |
| `codex_accounts` | `accounts` |
| `model` | `agent_parameters.model` |
| `reasoning_effort` | `agent_parameters.reasoning_effort` |
| `http_read_timeout_seconds` | `agent_parameters.http_read_timeout_seconds` |
| `supervisor_inactivity_timeout_seconds` | `agent_parameters.supervisor_inactivity_timeout_seconds` |
| `recovery_delay_seconds` | `agent_parameters.recovery_delay_seconds` |
| `max_recovery_attempts` | `agent_parameters.max_recovery_attempts` |
| `codex_account_fast_service_tier` | `agent_parameters.codex_account_fast_service_tier` |

Do not specify the same parameter in both locations, even with equal values.
Different parameters may use different locations during migration. OpenCode and
API-key configurations must use `agent_parameters` directly; account aliases
`codex_accounts` and `opencode_accounts` are rejected for those backends. Both
ChatGPT backends use `accounts` in new configurations.

## Choose a run root

The controller accepts `--run-root`. Put the experiment's configuration and
account directories there, then run the controller from the source checkout:

```bash
python3 run_controller.py --run-root /path/to/experiment
```

| Path under the run root | Purpose |
| --- | --- |
| `run_config.yaml` | Default configuration when `--run-config` is omitted |
| `codex_accounts/<account>/` | Codex ChatGPT account data |
| `opencode_chatgpt_accounts/<account>/` | OpenCode ChatGPT account data |
| `run/` | Workspaces, observations, logs, and `docker_images.json` |
| `run_tb/` | Terminal-Bench runtime socket, logs, and private results |

Without `--run-root`, these paths are under the caller's working directory at
startup. You can therefore run from an experiment directory using the controller's
absolute path:

```bash
cd /path/to/experiment
python3 /path/to/envtraverse_v0.1/run_controller.py
```

Relative `--run-root` and explicit `--run-config` arguments resolve against the
caller's working directory. An explicit configuration path never changes the run
root. Relative `server_mounts[].source` paths inside the YAML resolve against the
run root, even when the configuration file is elsewhere. Absolute mount sources
retain their specified locations.

`--accounts-dir /path/to/accounts` overrides the account parent directory for
either ChatGPT backend without moving or copying credentials.

Agent and environment source, Docker build files, account helper scripts, and
Terminal-Bench datasets and virtual environment stay in the source checkout.
The root controller scripts load their shared helpers from `src/controller/`.
For example, prepare an external account with:

```bash
./codex_accounts/prepare_account.sh /path/to/experiment/codex_accounts/codex01
```

For Terminal-Bench, the wrapper passes the same root to the runtime and controller:

```bash
python3 run_controller_tb.py --run-root /path/to/experiment --benchmarks 4.0
```

With manual runtime startup, pass the same `--run-root` to `host_runtime/run.sh`
and the selected controller. Existing outputs are checked only in the selected
run root.

## Choose an agent

Change the `agent` field in your configuration:

| Agent | `agent` value | Intended environments |
| --- | --- | --- |
| Baseline | `agent_v1` | All bundled environments |
| Stop-and-report | `agent_v1_01_wstop` | All bundled environments |
| Stop after repeated inactivity | `agent_v1_08_wstop3` | All bundled environments |
| Controlled_RESET | `test_arc3_agent_v1_02_wstop_wreset` | ARC-AGI-3 adapters that emit `metadata.txt` |

For example, set `agent: agent_v1_01_wstop` to run Stop-and-report with the same
tasks. Compare agents in separate experiments with the same environment settings.
See [Agents](../../src/agents/README.md) for the implementations and integration
instructions.

### Agent Docker build arguments

The controller accepts an optional `agent_docker_build_args` mapping. Each entry is
passed as `--build-arg NAME=VALUE` when building the selected agent's Docker image.
For example, `agent_v1_08_wstop3` supports selecting the Codex CLI version:

```yaml
agent: agent_v1_08_wstop3
agent_docker_build_args:
  CODEX_VERSION: "0.154.0"
```

The mapping defaults to `{}`, which preserves the selected Dockerfile's defaults.
The agents `agent_v1`, `agent_v1_01_wstop`, and `agent_v1_08_wstop3` accept
`CODEX_VERSION` and default to `0.154.0`. The agent
`test_arc3_agent_v1_02_wstop_wreset` retains its fixed version and does not accept
`CODEX_VERSION`.
Use an exact version for reproducible runs.

Names must match `ARG` declarations in the selected Dockerfile and values must be
strings; quote numeric values in YAML. `USER_ID` and `GROUP_ID` are managed by the
runner and cannot be overridden. Invalid settings fail before images are built.
Arguments apply only to the agent image; server, proxy, and account-tools builds
use their existing settings. Supplied arguments are recorded in `run/runner.log`.

## Choose an environment

`environment` selects an adapter. The `games` list contains its task IDs, including
for tasks that are not games. Optional `environment_parameters` configure that
adapter; `environment_variables` lists host variable names to forward to its
server. Both can be omitted when unused.

### ARC-AGI-3

Edit the agent, game IDs, model, and effort in
[run_config_arc3.yaml](../../run_config_examples/run_config_arc3.yaml), then run:

```bash
export ARC_API_KEY='your-arc-api-key'
python3 run_controller.py --run-config run_config_examples/run_config_arc3.yaml
```

The supplied configuration forwards `ARC_API_KEY` to the server and uses
`competition: false`. For competition runs, set
`environment_parameters.competition: true`. See the
[ARC-AGI-3 adapter](../../src/server/environments/arc3/README.md) for its settings.

### ARC-AGI-3 observation variants

These adapters use a minimal task description and the same game IDs and
credentials as the ARC-AGI-3 adapter above. Choose the observation format with
the `environment` field:

| Environment | Observations |
| --- | --- |
| [arc3_minimal_all](../../src/server/environments/arc3_minimal_all/README.md) | ASCII frames, PNG images, and metadata |
| [arc3_minimal_ascii](../../src/server/environments/arc3_minimal_ascii/README.md) | ASCII frames and metadata |
| [arc3_minimal_png](../../src/server/environments/arc3_minimal_png/README.md) | PNG images and metadata |
| [arc3_minimal_raw](../../src/server/environments/arc3_minimal_raw/README.md) | Raw SDK observations and numeric frame arrays in JSON |
| [arc3_minimal_img_wnoise3](../../src/server/environments/arc3_minimal_img_wnoise3/README.md) | JPEG or PNG images with configurable noise and deformation, plus metadata |
| [arc3_minimal_img_wnoise3_whighlight2](../../src/server/environments/arc3_minimal_img_wnoise3_whighlight2/README.md) | The same image settings, plus a pixel-highlight action |
| [arc3_minimal_img_wnoise4](../../src/server/environments/arc3_minimal_img_wnoise4/README.md) | Noisy, deformed images with configurable rotation after each action, plus metadata |

For the first four formats, make a copy of
[run_config_arc3.yaml](../../run_config_examples/run_config_arc3.yaml) and change `environment` to
the selected package name. Keep the game IDs, agent settings, and
`environment_variables: [ARC_API_KEY]` entry. Use the Baseline or Stop-and-report
agent with raw JSON; Controlled_RESET requires `metadata.txt`, which the raw
adapter does not emit.

The image-noise variants require explicit image and noise settings, including
fields in disabled groups. Start with the supplied configurations:

- [run_config_arc3_minimal_img_wnoise3.yaml](../../run_config_examples/run_config_arc3_minimal_img_wnoise3.yaml)
  selects images with noise and deformation.
- [run_config_arc3_minimal_img_wnoise3_whighlight2.yaml](../../run_config_examples/run_config_arc3_minimal_img_wnoise3_whighlight2.yaml)
  adds the `highlight_pixel` action and cursor-size setting.
- [run_config_arc3_minimal_img_wnoise4.yaml](../../run_config_examples/run_config_arc3_minimal_img_wnoise4.yaml)
  adds a rotation schedule before homography and deformation.

For example:

```bash
export ARC_API_KEY='your-arc-api-key'
python3 run_controller.py --run-config run_config_examples/run_config_arc3_minimal_img_wnoise3.yaml
```

Use another listed configuration with the same command to select its variant.
These configurations select `agent_v1`, game `ls20`, and non-competition mode.
The v3 examples produce 688×688 JPEG images from a 64×64 board; the v4 example
produces 896×896 images to accommodate rotation. Coordinates always refer to the
original board. Highlight actions advance the observation index without consuming
the game-solving budget.

### AGI Maze: individual labyrinths

Edit [run_config_agimaze.yaml](../../run_config_examples/run_config_agimaze.yaml) and run:

```bash
python3 run_controller.py --run-config run_config_examples/run_config_agimaze.yaml
```

Task IDs are paths relative to the bundled
[maze packs](../../src/server/environments/agimaze_single/labyrinth/packs/), for
example `TUTORIAL/STAGE-01/0000.json`. No environment API key is needed.

### AGI Maze: labyrinth sets

For a sequence of tutorials followed by a target maze, save the following as
`run_config_maze_set.yaml`:

```yaml
agent: agent_v1_01_wstop
agent_parameters:
  model: gpt-5.6-sol
  reasoning_effort: max
environment: agimaze_set_v1
games: [ts0_CLASSIC_EASY_pits_4x4]
accounts: [codex01]
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
python3 run_controller_tb.py --run-config run_config_examples/run_config_terminal_bench.yaml
```

The wrapper sets up the host runtime, starts the subscription controller, and
waits for grading and cleanup. The supplied
[small configuration](../../run_config_examples/run_config_terminal_bench.yaml) has two tasks;
[run_config_terminal_bench_2.1.yaml](../../run_config_examples/run_config_terminal_bench_2.1.yaml)
contains the full supplied task list.

To run all 70 Science 0.1 tasks from [run_config_terminal_bench_science_0.1.yaml](../../run_config_examples/run_config_terminal_bench_science_0.1.yaml):

```bash
python3 run_controller_tb.py --benchmarks science-0.1 --run-config run_config_examples/run_config_terminal_bench_science_0.1.yaml
```

The Science configuration uses `agent_v1_08_wstop3` with an HTTP read timeout of
36300 seconds and a supervisor inactivity timeout of 39600 seconds. All four
bundled agents accept these settings in `agent_parameters`; their defaults are 3900 and 7200
seconds. See the [agent settings](../../src/agents/README.md) for details.

`--benchmarks` accepts `2.1`, `3.0`, `4.0`, `science-0.1`, a comma-separated
combination, or `all`. It selects datasets for setup and checks that the configured
games belong to that selection. The `games:` list determines which tasks run.
Without the option, setup continues to select TB2.1.

Keep the `server_mounts` socket entry in these configurations. Archive both
`run/` and `run_tb/` in the selected run root before another experiment. See the
[Terminal-Bench running guide](../../README_tb.md) for setup details, task
discovery, and API-key runs.

## Run more tasks concurrently

Each ChatGPT backend uses one worker per entry in `accounts` and queues
remaining tasks. Prepare another account directory:

```bash
./codex_accounts/prepare_account.sh codex_accounts/codex02
```

Then set `accounts: [codex01, codex02]`. Each directory is authenticated
separately; directories using the same subscription share its usage limits.
The controller reserves all selected accounts for its lifetime using OS locks.
Standalone account commands use the same locks. A busy account fails immediately;
after abrupt controller death, an active Docker container mounting the account
also blocks reuse until stopped. Lock files stay on disk after release and should
not be deleted. Codex needs no persistent recovery marker. OpenCode additionally
preserves its database until per-game usage has been saved; see its
[usage recovery instructions](../../opencode_chatgpt_accounts/README.md#cleaning-an-account).

The optional `codex_account_fast_service_tier: true` in `agent_parameters` enables
the fast service tier for Codex subscription runs. Its default is `false`; the supplied Terminal-Bench
2.1 configurations enable it, while the Science configuration leaves it disabled.

## Run with an API key

API-key runs may be **substantially** more expensive than runs backed by
authenticated Codex accounts.

Set `backend: codex_openai_api_key` in the configuration. The
[API-key example](../../run_config_examples/run_config_api_key.yaml) runs the counter task:

```bash
export OPENAI_API_KEY='your-openai-api-key'
python3 run_controller.py --run-config run_config_examples/run_config_api_key.yaml
```

The runner checks Codex with the API key and asks for `continue` before launching.
Account lists are unused. By default all listed tasks can run concurrently;
set `max_parallel_games: 2`, for example, to run at most two games at once. The
limit includes server startup and game shutdown, and does not limit individual
model requests or subagents. By default, each agent container has a 10 GiB memory
limit; environment servers and Terminal-Bench task containers use additional
resources.

`OPENAI_API_KEY` authenticates the agent. Keep environment credentials in the
environment's own `environment_variables` configuration. For Terminal-Bench,
follow the [manual runtime instructions](../../README_tb.md#api-key-runs-and-manual-runtime)
before starting this runner.

## Find and keep the results

With the bundled agents, each task receives a generated alias and fresh files.
All paths below are relative to the selected run root:

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
