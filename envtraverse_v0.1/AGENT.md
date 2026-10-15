# Agent

Our agent is based on unsandboxed Codex. In principle, this means the Codex process can read any file that exists inside its runtime environment. Because of that, we treat the filesystem visible to the agent as part of the task definition: it must not contain hidden game implementation, previous run state, or any information that would let the agent cheat instead of learning from client observations.

- We run the agent in an isolated Docker container.
- We isolate it from the `arc_agi` library through a client/server boundary. The server that uses `arc_agi` runs in a separate Docker container.
- Before each new agent run, we remove previous Codex account state except for required authentication data such as `auth.json`.
- The [`src/agents/agent_v1/agent`](src/agents/agent_v1/agent) folder contains all information available to the agent.

# Selecting and preparing an agent

Every run config selects an agent explicitly:

```yaml
agent: agent_v1
```

An implementation lives in `src/agents/<name>/` and supplies a `Dockerfile` and a host-side `prepare_run.py`. Both controller variants build that Dockerfile with `src/` as the build context. For a manual build, use `./src/build_dockers.sh example agent_v1`.

The controller calls the selected hook once per game, after the server is ready and before launching the agent container:

```python
prepare_run(
    run_dir,             # Path to run/<game_alias>; server/ already exists
    server_url,          # URL reachable from the agent container
    game_alias,          # Public alias, not the real task ID
    parsed_run_config,   # Full parsed YAML mapping; treat it as read-only
    initial_files_dir=None,  # Optional environment-provided public helpers
) -> LaunchPreparation
```

[`LaunchPreparation`](agent_api.py) contains `command` (an argv list), `mounts` (a list of `AgentMount(source, target, read_only=False)`), and `environment` (a string-to-string mapping). Mount sources are absolute host paths. The agent Dockerfile sets `WORKDIR`; a mount only makes files available and does not select the working directory.

The hook prepares agent-owned files inside `run_dir`, interprets its own config settings, and decides where to place public helpers. The full config stays on the host: it contains real game IDs and environment settings that must not be copied into the agent workspace. The controller continues to manage Codex accounts/API keys, networking, resource limits, server lifecycle, scheduling, and cleanup.

[`agent_v1/prepare_run.py`](src/agents/agent_v1/prepare_run.py) preserves the existing `run`, `client`, and `observations` subdirectories, mounts, permissions, and command. Both `model` and `reasoning_effort` must be explicitly specified as non-empty strings in the run config; neither has a default. Its Dockerfile keeps `WORKDIR /home/user/run`.

# Contents of [`src/agents/agent_v1/agent`](src/agents/agent_v1/agent)

[`agent.py`](src/agents/agent_v1/agent/agent.py) is the external controller. It prepares the run directory, starts Codex, sends prompts to Codex, runs the client, inspects progress, and decides which prompt to send next.

The other top-level Python files are helper libraries and debugging tools used by [`agent.py`](src/agents/agent_v1/agent/agent.py): running Codex, reading session status, logging prompts, taking git snapshots, etc.

[`prompts/`](src/agents/agent_v1/agent/prompts) contains the prompts that [`agent.py`](src/agents/agent_v1/agent/agent.py) sends to Codex during the run.

This version ships without `src/agents/agent_v1/agent/workspace_init/`. The directory remains optional: if supplied, its contents are copied into `agent_run/` before starting Codex. The launcher creates a per-game `workspace_init/` only when the selected environment supplies public helpers in `to_agent_workspace/`. Otherwise the agent starts with an empty workspace. The main prompt stays in `prompts/`; no workspace symlink is needed.

[`client/`](src/agents/agent_v1/agent/client) is copied separately and mounted at `/client/`, alongside a per-game `/observations/` mount. Client code, session state, observations, and `/observations/environment_description.md` are read-only at rest. The client briefly toggles directory permissions while saving; this discourages accidental edits but is not a security boundary. Agent notes and transformed observations belong in the writable workspace.

For a more agent-facing explanation of these files, read [`prompts/main_prompt.md`](src/agents/agent_v1/agent/prompts/main_prompt.md). It describes the provided tools and expected deliverables in the same terms Codex sees during a run.

## Helper programs provided to the agent

- [`/client/client.py`](src/agents/agent_v1/agent/client/client.py) - live game CLI: start, move, status, environment-description.
- [`ascii_to_png.py`](src/server/environments/arc3/to_agent_workspace/ascii_to_png.py) - optional ASCII frame to PNG converter supplied by the selected environment.
