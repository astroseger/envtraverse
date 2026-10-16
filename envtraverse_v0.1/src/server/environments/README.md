# Environments

An environment adapter connects a task implementation to EnvTraverse's
[interaction contract](../../../README.md#common-interaction-interface).
It supplies the task description, interprets JSON actions, and returns observation
files and terminal status. The shared server implements the
[HTTP protocol](../README.md); adapters do not need to implement HTTP themselves.

Tasks must be expressible through requests initiated by the agent and observations
returned in response. This includes games, simulations, and terminal tasks. An
environment may evolve between requests, use external services, or run additional
processes. It does not push unsolicited observations to the agent.

## Existing adapters

| Package | Task and useful example |
| --- | --- |
| [example](example/README.md) | A counter held in memory; the simplest starting point |
| [arc3](arc3/README.md) | ARC-AGI-3 through its SDK; text and image observations |
| [agimaze_single](agimaze_single/README.md) | One labyrinth; manages a local engine subprocess |
| [agimaze_set_v1](agimaze_set_v1/README.md) | An ordered sequence of labyrinths in one session |
| [terminal_bench](terminal_bench/README.md) | Terminal tasks backed by a separate host runtime |

For commands and configurations, see
[Running experiments](../../../docs/running/README.md#choose-an-environment).

## Add an environment

Start by copying [example/](example/) to `src/server/environments/<name>/`.
Use a directory name that is a Python identifier. Its `environment_manager.py`
must export an `EnvironmentManager` class with the following methods:

| Method | Responsibility |
| --- | --- |
| `__init__(parameters)` | Store and validate the `environment_parameters` mapping from the run configuration |
| `start_environment(game_id)` | Start the selected task and return its description and initial observation |
| `move(action)` | Apply one JSON action and return an observation |
| `stop()` | Release the task's resources, including after failed startup |

No base class or registry entry is needed. The server discovers packages with
`environment_manager.py`. Use relative imports for helpers within your package.

Import the result types from [environment_api.py](../environment_api.py):

```python
from environment_api import Observation, StartResult

# Returned by start_environment():
return StartResult(
    environment_description="Reach the target. Send {\"id\":\"INC\"} to increment.",
    observation=Observation(files={"state.txt": "value: 0"}),
)
```

`move()` returns an `Observation` directly:

```python
return Observation(files={"state.txt": "value: 3"}, terminal=True)
```

File contents are strings or bytes; names are plain filenames without directory
components. An observation can contain several files or none. The server handles
encoding them for HTTP. Termination means that no further actions are accepted;
the environment defines how success is reported.

Put backend initialization in `start_environment()`. Each manager serves one
session and permits one start attempt. Make `stop()` safe before startup, after
failure, and when called repeatedly. A new run gets a new manager and server.

## Describe the task and its information boundary

The description should explain the goal, available actions, observation formats,
and termination conditions. Decide which rules and state the agent is allowed
to observe. Keep that specification fixed when comparing agents.

Optional `to_agent_workspace/` files are passed to the agent's preparation hook
before launch. Use this directory for public documentation, helper programs, or
initial data. The HTTP server does not distribute these files. Keep private task
implementation, answers, credentials, and evaluation results out of the agent's
workspace and responses.

An action may also refer to files in the agent's workspace if the environment's
infrastructure supports this. For example, Terminal-Bench's
[`send_path`](terminal_bench/README.md#files-from-the-agent-workspace) copies files
into its task container. This requires explicit integration; the shared server
does not automatically mount the agent workspace.

## Dependencies and configuration

Add Python dependencies to an optional package-local `requirements.txt`; the
runner installs them in the server image. Backends needing extra system packages
or external infrastructure may also need changes to the server image or runner.

Select your package and tasks in a run configuration:

```yaml
environment: my_environment
games: [task01, task02]
environment_parameters:
  seed: 42
environment_variables: [MY_ENVIRONMENT_KEY]
```

This is an environment fragment: also select an agent and its run settings as in
the [running guide](../../../docs/running/README.md). `games` contains private task
IDs understood by your adapter; the agent receives a generated alias. Parameters
are ordinary configuration values. `environment_variables` lists host environment
variable names to forward to the server; export their values before running.

## Report errors

Use the exceptions in [environment_api.py](../environment_api.py):

- `InvalidActionError`: reject an action before changing state.
- `EnvironmentStartError`: the task could not start.
- `EnvironmentError`: an operation failed, but the backend can still be used.
- `FatalEnvironmentError`: the backend is unusable; end the server session.

These exception messages are visible to the agent. Keep private backend details
in server logs or chained exceptions. A failed action does not advance the
server's step counter, but an operational failure may occur after the backend
has changed. The server does not automatically replay actions.

Use the [direct server example](../README.md#try-the-protocol-locally) to try an
adapter without an LLM. The shared server and environment tests can be run from
`envtraverse_v0.1/` with `python3 -m unittest discover -s src/server/tests -v`.
