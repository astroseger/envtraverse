# EnvTraverse

EnvTraverse is an experimental framework for scientific studies of agents across
interactive environments. Agents and environments communicate through a common
contract: an environment description, JSON actions, and observation files.

Each active task runs in its own agent and environment-server containers. Select
the agent and environment independently in a YAML run configuration. The bundled
agents use Codex; the framework interfaces also support other implementations.

All commands below run from `envtraverse_v0.1/`.

## Requirements

- Linux with Docker available to your user.
- Python 3.11+ and PyYAML on the host.
- For the bundled agents, an authenticated Codex account or an OpenAI API key.
- Any credentials required by the selected environment, such as `ARC_API_KEY`.
- For Terminal-Bench, Python 3.12+, Git, and Docker Compose v2; see the
  [Terminal-Bench guide](README_tb.md).

The controllers build the agent, server, and proxy images automatically.
Environment dependencies are installed in the server image.

## First run

Install the host configuration dependency and create an account directory:

~~~bash
python3 -m pip install PyYAML
./codex_accounts/prepare_account.sh codex_accounts/codex01
./codex_accounts/check_account.sh codex_accounts/codex01
~~~

Follow the device-authentication instructions. These scripts build their shared
account-tools image on first use. See [account tools](codex_accounts/README.md)
for more options.

The supplied [run_config.yaml](run_config.yaml) selects the baseline agent and a
small counter environment. A configuration with an explicit target looks like:

~~~yaml
agent: agent_v1
environment: example
environment_parameters:
  target: 3
games: [counter]
codex_accounts: [codex01]
model: gpt-5.6-sol
reasoning_effort: max
~~~

The bundled agents require both `model` and `reasoning_effort`; set them to the
configuration you want to evaluate. The example environment needs no environment
API key, but running its agent still uses Codex.

Start the experiment:

~~~bash
python3 run_controller.py --run-config run_config.yaml
~~~

Output goes to `run/`. Archive or rename an existing `run/` before starting
another experiment. The output location is fixed relative to this framework
directory, including when you pass a different config file. For long runs, use
`tmux` or `screen`.

## Choose an agent

Set `agent` to the directory name under [src/agents/](src/agents/):

| Agent | YAML value | Environments |
| --- | --- | --- |
| [Baseline](src/agents/agent_v1/) | `agent_v1` | Any supported environment |
| [Stop-and-report](src/agents/agent_v1_01_wstop/) | `agent_v1_01_wstop` | Any supported environment |
| [Controlled_RESET](src/agents/test_arc3_agent_v1_02_wstop_wreset/) | `test_arc3_agent_v1_02_wstop_wreset` | ARC-AGI-3 only |

Baseline and Stop-and-report use the same general tools and controller, with a
different continuation instruction. Controlled_RESET adds ARC-AGI-3-specific
reset management.

For example, change `agent: agent_v1` to `agent: agent_v1_01_wstop` in any of
the supplied configurations to run Stop-and-report. Each experiment selects one
agent; compare agents in separate runs with the same environment settings.

## Choose an environment

`environment` selects a package under
[src/server/environments/](src/server/environments/). Despite the configuration
key's name, `games` contains task IDs for any environment.

| Environment | YAML value | Task IDs and example configuration |
| --- | --- | --- |
| [Counter example](src/server/environments/example/README.md) | `example` | `counter`; [run_config.yaml](run_config.yaml) |
| [ARC-AGI-3](src/server/environments/arc3/README.md) | `arc3` | Game IDs such as `ls20`; [run_config_arc3.yaml](run_config_arc3.yaml) |
| [AGI Maze: single labyrinth](src/server/environments/agimaze_single/README.md) | `agimaze_single` | Paths relative to the bundled `packs/`; [run_config_agimaze.yaml](run_config_agimaze.yaml) |
| [AGI Maze: labyrinth set](src/server/environments/agimaze_set_v1/README.md) | `agimaze_set_v1` | Set filenames without `.json`; example below |
| [Terminal-Bench](README_tb.md) | `terminal_bench` | Versioned task IDs; [small example](run_config_terminal_bench.yaml) or [full 2.1 task list](run_config_terminal_bench_2.1.yaml) |

Optional environment settings are:

- `environment_parameters`: a mapping of settings accepted by that environment;
  defaults to `{}`.
- `environment_variables`: names of host variables to forward to the server;
  defaults to `[]`. Export their values in the shell.
- `server_mounts`: host directories mounted into the server, used for the
  Terminal-Bench runtime socket. Relative sources resolve against this framework
  directory.

The environment provides its description and optional workspace helpers.
The same general agent can run across environments without changes to its code
or prompts.

### ARC-AGI-3

Edit the agent, games, model, and effort in
[run_config_arc3.yaml](run_config_arc3.yaml), then run:

~~~bash
export ARC_API_KEY='your-arc-api-key'
python3 run_controller.py --run-config run_config_arc3.yaml
~~~

The supplied configuration uses `competition: false`. For a competition run,
set `environment_parameters.competition: true` and optionally set `tag` in
that mapping. Both modes can require the API key and access to game downloads.
The key is forwarded to the environment server.

### AGI Maze

For independent labyrinths:

~~~bash
python3 run_controller.py --run-config run_config_agimaze.yaml
~~~

Use exact maze filenames from the bundled
[packs](src/server/environments/agimaze_single/labyrinth/packs/), such as
`TUTORIAL/STAGE-01/0000.json`.

For a sequence of tutorials followed by a target maze, save a configuration such
as `run_config_maze_set.yaml`:

~~~yaml
agent: agent_v1_01_wstop
environment: agimaze_set_v1
games: [ts0_CLASSIC_EASY_pits_4x4]
codex_accounts: [codex01]
model: gpt-5.6-sol
reasoning_effort: max
~~~

~~~bash
python3 run_controller.py --run-config run_config_maze_set.yaml
~~~

The agent retains its workspace and conversation across mazes within a set.
Choose other sequences from the bundled
[set definitions](src/server/environments/agimaze_set_v1/sets/).
Neither maze environment requires an environment API key.

### Terminal-Bench

Terminal-Bench also needs a host runtime that manages task containers and grading.
After configuring the agent and accounts, run:

~~~bash
python3 run_controller_tb.py --run-config run_config_terminal_bench.yaml
~~~

The wrapper runs host setup, starts the runtime, launches the account-based
controller, and waits for grading and cleanup. Use
`run_config_terminal_bench_2.1.yaml` for the full supplied task list. Keep the
socket mount from the example configuration, and archive both `run/` and
`run_tb/` before another experiment.

The [Terminal-Bench guide](README_tb.md) covers task discovery, manual runtime
startup, and environment-specific requirements.

## Accounts, concurrency, and API authentication

The account-based controller uses one worker per entry in `codex_accounts` and
queues additional tasks. Create another account directory for each concurrent
worker:

~~~bash
./codex_accounts/prepare_account.sh codex_accounts/codex02
~~~

Then use `codex_accounts: [codex01, codex02]`. Each directory is authenticated
separately; directories authenticated to the same subscription share its usage
limits. Use dedicated account directories for concurrent experiments, since the
controller clears saved Codex state before each task while retaining authentication.

`codex_account_fast_service_tier: true` enables the fast service tier for
account-based runs; the default is `false`. Both supplied Terminal-Bench configs
enable it.

To use API authentication:

~~~bash
export OPENAI_API_KEY='your-openai-api-key'
python3 run_controller_with_api_key.py --run-config run_config.yaml
~~~

The API controller uses the same agent and environment configuration, ignores
`codex_accounts`, and launches all listed tasks in parallel. It asks you to type
`continue` before starting. Keep the task list within the host's capacity.
`OPENAI_API_KEY` is the agent credential; `environment_variables` is for
credentials needed by the environment.

For Terminal-Bench with API authentication, start the host runtime manually as
described in [README_tb.md](README_tb.md), then use the API controller with the
Terminal-Bench configuration.

## Run files and lifecycle

The bundled agents use this layout:

| Host path | Contents |
| --- | --- |
| `run/runner.log` | Experiment scheduler and container-launch log |
| `run/docker_images.json` | Image tags for this experiment |
| `run/<alias>/client/` | Client code, configuration, and session state |
| `run/<alias>/observations/` | Environment description, observations, and submitted actions |
| `run/<alias>/run/` | Agent code and logs; `agent_run/` is its writable workspace |
| `run/<alias>/server/` | Server log and environment outputs |
| `run_tb/` | Additional host-runtime output for Terminal-Bench |

Tasks receive random aliases. Each task has a fresh workspace. The agent has no
direct mount of the environment implementation or server outputs. Agent Internet
access goes through the shared allowlisted proxy; environments may
provide further capabilities through their actions. Containers share an internal
network, so the setup assumes nonadversarial agents.

For the bundled agents, the supervisor restarts the agent controller if it exits
before task termination or `agent.log` is inactive for two hours, up to ten
recovery attempts. Recovery
requires saved client state and retains the environment and workspace. At step
zero it starts a new Codex conversation; after that it resumes the saved one.
Codex stderr is saved separately in `codex_stderr.log`.

Each agent container has a 10 GiB memory cap and a 2 GiB per-process data limit.
Allow additional resources for environment servers and, for Terminal-Bench, task
containers and background grading.

Ctrl+C requests cleanup of active containers and shared networking. Normal
cleanup also removes the experiment's image tags and restores owner write
permissions on its output files. A forced kill or host crash can leave resources
behind. The controllers build separate image tags for each experiment; manual
builds are also possible with both the environment and agent specified:

~~~bash
./src/build_dockers.sh arc3 agent_v1_01_wstop
~~~

## Analysis

See [analysis_scripts/README.md](analysis_scripts/README.md) for action and token
extraction, benchmark reports, replay, and run diagnostics.

## Extending the framework

See [AGENT.md](AGENT.md) for the agent preparation interface and
[src/server/README.md](src/server/README.md) for environment interfaces and
direct server/client usage.
