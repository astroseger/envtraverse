# Agents

An EnvTraverse agent reads an environment description, chooses actions, and
interprets observations through the common
[HTTP protocol](../server/README.md). Its strategy, tools, memory, and use of a
model belong to the agent implementation. The interaction contract does not
require an LLM or an environment-specific strategy.

For running the supplied agents, see
[Running experiments](../../docs/running/README.md). This guide describes how an
agent connects to the framework.

## Bundled agents

All three use Codex and provide an environment-agnostic client to the LLM.

| Agent | Scope |
| --- | --- |
| [agent_v1](agent_v1/) | Environment-agnostic |
| [agent_v1_01_wstop](agent_v1_01_wstop/) | Environment-agnostic; changes the instruction for returning control |
| [test_arc3_agent_v1_02_wstop_wreset](test_arc3_agent_v1_02_wstop_wreset/) | ARC-AGI-3-specific reset management |

The agent controller starts Codex, supplies prompts, and resumes it as needed.
Codex uses its workspace and the client to solve the task. The client saves the
description, actions, and observations, but does not choose a strategy.

## Integrate an agent

Create `src/agents/<name>/` with two entry points:

- **`Dockerfile`** defines the agent's software and working directory. The build
  context is `src/`; the runner supplies the host user's `USER_ID` and `GROUP_ID`
  as build arguments.
- **`prepare_run.py`** prepares the files for one task and returns the command,
  mounts, and environment variables needed to launch the agent container.

Use [agent_v1/prepare_run.py](agent_v1/prepare_run.py) as a working example. The
framework calls the hook on the host after the server is ready:

```python
def prepare_run(
    run_dir,
    server_url,
    game_alias,
    parsed_run_config,
    initial_files_dir=None,
) -> LaunchPreparation:
    ...
```

`run_dir` is this task's output directory, with `server/` already present.
`server_url` is reachable from the agent container. `game_alias` is the public
identifier used to start the session. `initial_files_dir` contains optional
public files supplied by the environment; make them available in the agent's
workspace.

Return a [LaunchPreparation](../../agent_api.py) with an argv `command`, any
`AgentMount` entries, and an `environment` mapping. The hook decides how to pass
the server URL and alias to its agent: a configuration file, command arguments,
or environment variables are all possible.

The full `parsed_run_config` is available to the host hook so it can read the
agent's settings. Copy only the settings intended for the agent into its
container: the full mapping also contains private task IDs and environment
configuration. Use fresh per-task files, and build an image containing only what
the agent should be able to access.

Select the package with `agent: <name>` in the run configuration. The supplied
runners handle task scheduling, servers, networking, and cleanup. The agent
process should exit when its session is over.

## Communicate with the environment

The bundled agents use [client.py](agent_v1/agent/client/client.py), documented
in the [client README](agent_v1/agent/client/README.md). Their preparation hooks
write the server URL to `client_config.json` and mount that directory at
`/client/`. The client saves observations under `/observations/`; the writable
working directory is `/home/user/run/agent_run/`.

The client is optional. An agent can issue HTTP requests directly or provide a
different client, following the [server protocol](../server/README.md). It must
start the session with its alias, retain the returned session token, submit
actions, and handle observation files and terminal status.

The server can return the last successful response after an interrupted request.
Use that to check what happened before repeating an action; actions are not
automatically safe to replay.

For Terminal-Bench file uploads, the host runtime currently expects the writable
workspace at `run/<alias>/run/agent_run/`. An agent with a different layout needs
to provide that location or adapt the integration; see
[files from the agent workspace](../server/environments/terminal_bench/README.md#files-from-the-agent-workspace).

## Runners and deliberate isolation

The supplied runners authenticate Codex through subscription accounts or an
OpenAI API key. An agent using another provider or local inference needs an
adapted runner, for example to change authentication or expose a GPU or inference
service. Shared scheduling and container helpers are in
[run_funs.py](../../run_funs.py).

Agent internet access is deliberately restricted to an allowlisted model-service
proxy to reduce unintended information channels during evaluation. The runners
set the standard proxy environment variables and exempt the environment server
from proxying. The current allowlist is in
[Dockerfile.proxy](../Dockerfile.proxy). Adapt a runner and its proxy settings
when the study requires different access, and treat that access policy as part
of the experimental setup.

Environments can expose additional capabilities through their actions. Keep
environment implementations, private results, and unrelated run state outside
the agent's accessible files.
