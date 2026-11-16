# Running experiments

Use this guide to choose a configuration, prepare an account, run an experiment,
and find its results. For how agents and environments interact, see the
[framework README](../../README.md).

Unless stated otherwise, the commands below assume you are in the repository
directory, `envtraverse_v0.1/`.

## Requirements

- Linux with Docker available to your user.
- Python 3.11+ and PyYAML on the host.
- A ChatGPT account for Codex or OpenCode, or an OpenAI API key for Codex.
- Any credentials required by the environment, such as `ARC_API_KEY`.
- For Terminal-Bench: Python 3.12+, Git, and Docker Compose v2.

Install the controller's Python dependency:

```bash
python3 -m pip install PyYAML
```

The runners build the agent, server, and proxy images automatically, including
the selected environment's Python dependencies.

## Choose a backend

The configuration's `backend` selects how the agent signs in. Choose the option
that matches the credentials you want to use:

| `backend` | Credentials | Agent family | Games running at once |
| --- | --- | --- | --- |
| `codex_chatgpt_accounts` | ChatGPT accounts in `codex_accounts/` | Codex | One per account |
| `opencode_chatgpt_accounts` | ChatGPT accounts in `opencode_chatgpt_accounts/` | OpenCode | One per account |
| `codex_openai_api_key` | `OPENAI_API_KEY` in your shell | Codex | All configured games, or a `max_parallel_games` limit |

Account directories are under your [experiment directory](#choose-a-run-root)
unless you select another location with `--accounts-dir`. Omitting `backend`
selects `codex_chatgpt_accounts`.

Follow the Codex example below, the [OpenCode instructions](#run-with-opencode-chatgpt),
or the [API-key instructions](#run-with-an-api-key).

Each setup example describes a separate experiment. After copying a new example,
apply your account and model settings to the new file; it does not inherit edits
from a previous configuration.

## First run with Codex ChatGPT

Prepare an account directory:

```bash
./codex_accounts/prepare_account.sh codex_accounts/codex01
./codex_accounts/check_account.sh codex_accounts/codex01
```

Follow the device-authentication instructions. The scripts build their account
tools image on first use; see [account tools](../../codex_accounts/README.md).

Start with the [counter example](../../run_config_examples/run_config.yaml).
Copy it to `run_config.yaml`, the file the controller reads by default:

```bash
cp run_config_examples/run_config.yaml run_config.yaml
```

Edit the copy to choose your account, model, and reasoning effort. For example:

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
python3 run_controller.py
```

The controller builds the required images, checks the account, and starts the
configured task. Results go to `run/` in your current directory. See
[Find and keep the results](#find-and-keep-the-results) for the output files.

Before another experiment, archive the previous `run/` or choose a fresh
[experiment directory](#choose-a-run-root). For long experiments, use `tmux`
or `screen`.

## Run with OpenCode ChatGPT

OpenCode uses its own account directory. Prepare one and follow the login
instructions:

```bash
./opencode_chatgpt_accounts/prepare_account.sh opencode_chatgpt_accounts/account01
cp run_config_examples/run_config_opencode.yaml run_config.yaml
```

Edit your copy: set `accounts: [account01]`, choose `agent_parameters.model` and
`agent_parameters.reasoning_effort`, and keep these selections:

```yaml
backend: opencode_chatgpt_accounts
agent: opencode_agent_v1_08_wstop3
```

Then run:

```bash
python3 run_controller.py
```

The controller checks each account before running games. It uses the original
account directory so refreshed credentials are saved there. It clears history
between games while preserving credentials. See the
[OpenCode account guide](../../opencode_chatgpt_accounts/README.md) for account
commands and [OpenCode agent settings](../../src/agents/opencode_agent_v1_08_wstop3/README.md#configuration)
for available options.

## Run with an API key

API-key runs may be **substantially** more expensive than runs backed by
authenticated Codex accounts.

To try the counter task, copy the
[API-key example](../../run_config_examples/run_config_api_key.yaml), which selects
`backend: codex_openai_api_key`. Edit its model and effort before running:

```bash
cp run_config_examples/run_config_api_key.yaml run_config.yaml
# Edit run_config.yaml for your experiment.
export OPENAI_API_KEY='your-openai-api-key'
python3 run_controller.py
```

The controller asks you to type `continue` to confirm API-key use. It then builds
the images and checks the key before starting games. Account directories are
not required, and the `accounts` list is unused. To use another Codex
configuration, set its `backend` to `codex_openai_api_key`.

Omit `max_parallel_games` or set it to `all` to run all listed tasks concurrently.
Set a positive integer, such as `max_parallel_games: 2`, to limit active games.
The limit includes server startup and game shutdown, and does not limit individual
model requests or subagents. By default, each agent container has a 10 GiB memory
limit; environment servers and Terminal-Bench task containers use additional
resources.

`OPENAI_API_KEY` authenticates the agent. Keep environment credentials in the
environment's own `environment_variables` configuration. For Terminal-Bench,
follow the [manual runtime instructions](../../README_tb.md#api-key-runs-and-manual-runtime)
before starting the controller: the API-key confirmation requires an interactive
terminal, which `run_controller_tb.py` does not provide to its child controller.

## Agent parameters

Put model, reasoning effort, client and supervisor timeouts, recovery settings,
and native Codex or OpenCode options inside `agent_parameters`. Both `model` and
`reasoning_effort` are required by the supplied agents; optional settings retain
their defaults when omitted.

The configuration's `agent` field chooses which implementation uses those
settings. Custom agents may accept additional settings or structured values.
Keep `agent_docker_build_args` and `runner` resource limits outside
`agent_parameters`.

If you are reusing a configuration written for an earlier version, see
[Using older configurations](#using-older-configurations).

## Choose a run root

The **run root** is your experiment directory. It defaults to the directory
where you start the controller. Use `--run-root` to choose another location.

| Path under the run root | Purpose |
| --- | --- |
| `run_config.yaml` | Default configuration when `--run-config` is omitted |
| `codex_accounts/<account>/` | Codex ChatGPT account data |
| `opencode_chatgpt_accounts/<account>/` | OpenCode ChatGPT account data |
| `run/` | Workspaces, observations, logs, and `docker_images.json` |
| `run_tb/` | Terminal-Bench runtime socket, logs, and private results |

To keep a Codex experiment elsewhere while reusing the account you prepared
above, copy your edited configuration and point to the existing account directory:

```bash
mkdir -p /path/to/experiment
cp run_config.yaml /path/to/experiment/run_config.yaml
python3 run_controller.py \
  --run-root /path/to/experiment \
  --accounts-dir "$PWD/codex_accounts"
```

The copied configuration must list the accounts you prepared. For an OpenCode
configuration, use `--accounts-dir "$PWD/opencode_chatgpt_accounts"` instead.
`--accounts-dir` uses credentials in their existing location; you do not need to
copy account folders.

Alternatively, prepare accounts inside the experiment directory. For example:

```bash
./codex_accounts/prepare_account.sh /path/to/experiment/codex_accounts/codex01
```

Then `--accounts-dir` is unnecessary. From the experiment directory itself,
you can also omit `--run-root`:

```bash
cd /path/to/experiment
python3 /path/to/envtraverse_v0.1/run_controller.py
```

Configuration selection and output location are separate:

- `--run-config path/to/config.yaml` selects a file and leaves the run root unchanged.
- Relative command-line paths use the directory where you start the controller.
- Relative `server_mounts[].source` paths inside the YAML use the run root.

For example, selecting a file in `run_config_examples/` still saves results under
`run/` in your current directory. You can copy an example to a local
`run_config.yaml`, which is ignored by Git in this checkout, or pass the example
path directly after checking its account and model settings.

Source code, build files, account helper scripts, and downloaded Terminal-Bench
datasets stay in the repository. The Terminal-Bench controller uses the selected
run root for both the runtime and the experiment. For manual runtime startup,
pass the same `--run-root` to `host_runtime/run.sh` and `run_controller.py`; see
[the Terminal-Bench guide](../../README_tb.md).

## Choose an agent

Change the `agent` field in your configuration to select an implementation that
supports your backend. These are the main choices:

| Agent | `agent` value | Compatible backends | Intended environments |
| --- | --- | --- | --- |
| Codex baseline | `agent_v1` | `codex_chatgpt_accounts`, `codex_openai_api_key` | All bundled environments |
| Codex stop-and-report | `agent_v1_01_wstop` | `codex_chatgpt_accounts`, `codex_openai_api_key` | All bundled environments |
| Codex stop after repeated inactivity | `agent_v1_08_wstop3` | `codex_chatgpt_accounts`, `codex_openai_api_key` | All bundled environments |
| [OpenCode baseline](../../src/agents/opencode_agent_v1/README.md) | `opencode_agent_v1` | `opencode_chatgpt_accounts` | All bundled environments |
| OpenCode stop after repeated inactivity | `opencode_agent_v1_08_wstop3` | `opencode_chatgpt_accounts` | All bundled environments |
| Codex Controlled_RESET | `test_arc3_agent_v1_02_wstop_wreset` | `codex_chatgpt_accounts`, `codex_openai_api_key` | ARC-AGI-3 adapters that emit `metadata.txt` |

For a Codex backend, set `agent: agent_v1_01_wstop` to run Stop-and-report with
the same tasks. Compare agents in separate experiments with the same environment
settings. See [Agents](../../src/agents/README.md) for integration instructions
and the [OpenCode agent guide](../../src/agents/opencode_agent_v1_08_wstop3/README.md)
for its native options.

## Choose an environment

`environment` selects an adapter. The `games` list contains its task IDs, including
for tasks that are not games. Optional `environment_parameters` configure that
adapter; `environment_variables` lists host variable names to forward to its
server. Both can be omitted when unused.

### ARC-AGI-3

Copy [the ARC-AGI-3 example](../../run_config_examples/run_config_arc3.yaml) for a
new experiment. Edit the copy's accounts, agent, game IDs, model, and effort
before launching:

```bash
cp run_config_examples/run_config_arc3.yaml run_config.yaml
# Edit run_config.yaml for your experiment.
export ARC_API_KEY='your-arc-api-key'
python3 run_controller.py
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

For example, start an image-noise experiment with:

```bash
cp run_config_examples/run_config_arc3_minimal_img_wnoise3.yaml run_config.yaml
# Edit run_config.yaml, including your accounts, model, and effort.
export ARC_API_KEY='your-arc-api-key'
python3 run_controller.py
```

Copy another listed example to select its variant, then apply your experiment
settings to that copy. These configurations select `agent_v1`, game `ls20`, and
non-competition mode.
The v3 examples produce 688×688 JPEG images from a 64×64 board; the v4 example
produces 896×896 images to accommodate rotation. Coordinates always refer to the
original board. Highlight actions advance the observation index without consuming
the game-solving budget.

### AGI Maze: individual labyrinths

Copy [the AGI Maze example](../../run_config_examples/run_config_agimaze.yaml)
and edit it for your experiment:

```bash
cp run_config_examples/run_config_agimaze.yaml run_config.yaml
# Edit run_config.yaml, including your accounts, model, and effort.
python3 run_controller.py
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
For a new experiment with ChatGPT accounts, copy the small example and edit your
account and model settings:

```bash
cp run_config_examples/run_config_terminal_bench.yaml run_config.yaml
# Edit run_config.yaml for your experiment.
python3 run_controller_tb.py
```

The example selects Codex. For OpenCode, follow the backend and agent changes
in the [Terminal-Bench guide](../../README_tb.md). The wrapper sets up the host
runtime, starts the experiment controller, and waits for grading and cleanup. The
supplied
[small configuration](../../run_config_examples/run_config_terminal_bench.yaml) has two tasks;
[run_config_terminal_bench_2.1.yaml](../../run_config_examples/run_config_terminal_bench_2.1.yaml)
contains the full supplied task list.

To run all 70 Science 0.1 tasks from [run_config_terminal_bench_science_0.1.yaml](../../run_config_examples/run_config_terminal_bench_science_0.1.yaml):

```bash
cp run_config_examples/run_config_terminal_bench_science_0.1.yaml run_config.yaml
# Edit the new copy's accounts, model, and effort before running.
python3 run_controller_tb.py --benchmarks science-0.1
```

The Science configuration uses `agent_v1_08_wstop3` with an HTTP read timeout of
36300 seconds and a supervisor inactivity timeout of 39600 seconds. The supplied
agents accept these settings in `agent_parameters`; their defaults are 3900 and
7200 seconds. See the [agent settings](../../src/agents/README.md) for details.

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
remaining tasks. For Codex, prepare another account directory:

```bash
./codex_accounts/prepare_account.sh codex_accounts/codex02
```

Then set `accounts: [codex01, codex02]`. For OpenCode, use its account tools and
list the corresponding names from `opencode_chatgpt_accounts/`. Each directory
is authenticated separately; directories using the same subscription share its
usage limits.

An account can be used by only one running experiment or account command at a
time. If an account is busy, the command stops with an error. After an interrupted
run, stop any surviving container using the account before reusing it. Leave lock
files in place; their presence alone does not mean the account is busy. OpenCode
may also require saving the previous game's usage before account reuse; follow its
[usage recovery instructions](../../opencode_chatgpt_accounts/README.md#cleaning-an-account).

`max_parallel_games` is available only for the API-key backend. Including it in
either ChatGPT backend's configuration is an error, even with the value `all`.

The optional `codex_account_fast_service_tier: true` in `agent_parameters` enables
the fast service tier for Codex subscription runs. Its default is `false`; the
supplied Terminal-Bench 2.1 configurations enable it, while the Science
configuration leaves it disabled.

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

## Agent Docker build arguments

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

## Using older configurations

If you use one of the current examples, you can skip this section.

For `codex_chatgpt_accounts`, configurations from earlier versions may still put
agent settings at the top level of the YAML file. Those locations remain
supported, but use the following layout when updating a file:

| Old setting | Current setting |
| --- | --- |
| `codex_accounts` | `accounts` |
| `model` | `agent_parameters.model` |
| `reasoning_effort` | `agent_parameters.reasoning_effort` |
| `http_read_timeout_seconds` | `agent_parameters.http_read_timeout_seconds` |
| `supervisor_inactivity_timeout_seconds` | `agent_parameters.supervisor_inactivity_timeout_seconds` |
| `recovery_delay_seconds` | `agent_parameters.recovery_delay_seconds` |
| `max_recovery_attempts` | `agent_parameters.max_recovery_attempts` |
| `codex_account_fast_service_tier` | `agent_parameters.codex_account_fast_service_tier` |

For example, replace a top-level `model: ...` with `model: ...` inside
`agent_parameters`. Move each setting rather than keeping both copies; duplicate
settings are rejected even when their values match. You can move different
settings separately while updating a Codex ChatGPT configuration.

This compatibility applies only to Codex ChatGPT. OpenCode and API-key
configurations require `agent_parameters` directly. Both ChatGPT backends use
`accounts`; the old names `codex_accounts` and `opencode_accounts` are rejected
for OpenCode and API-key configurations.
