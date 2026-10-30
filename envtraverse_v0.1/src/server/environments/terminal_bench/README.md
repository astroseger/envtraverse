# Terminal-Bench environment

This adapter exposes terminal tasks through EnvTraverse's common interaction
interface. Each environment server connects over a Unix socket to a host runtime,
which manages the task containers and grading through Harbor. The agent continues
to use the same [HTTP protocol](../../README.md) as for other environments.

For installation and run examples, see the
[Terminal-Bench running guide](../../../../README_tb.md). For the shared adapter
interface, see [Environments](../README.md).

## Task interface

Initialization prepares the task and returns its instructions, solving time
budget, action reference, and initial observation. The task sandbox has a
separate filesystem from the agent's local workspace.

The environment exposes six actions:

| Action | Purpose |
| --- | --- |
| `terminal_send` | Send text or named keys to an interactive terminal |
| `terminal_read` | Read a terminal screen or recent history |
| `terminal_new` | Open another terminal in the same task container |
| `get_file` | Retrieve a file from the task container |
| `send_path` | Copy a file or directory from the agent workspace into the task container |
| `finish` | End solving and submit the task for grading |

Terminal commands keep running between requests. Sending input does not wait for
command completion; the agent inspects subsequent observations. The complete
action reference supplied to the agent is defined in [description.py](description.py).

Native solving and verifier time limits are preserved. Thinking and pauses
between actions count toward the solving clock; task preparation and grading do
not. The environment communicates the solving budget in its initial description.

## Datasets and task IDs

The catalog supports Terminal-Bench 2.1, 3.0, 4.0, and Terminal-Bench-Science 0.1.
Task IDs are the benchmark prefix followed by the task directory's relative path
under the checkout's `tasks/` directory. Nested paths use `/`; existing flat task
IDs keep their names. For example:

```text
terminal-bench-2.1-fix-git
terminal-bench-science-0.1-physical-sciences/astronomy/3x2pt-inference
```

Science uses its own checkout, pinned to `v0.1.0`, and the published membership in
`tasks/dataset.toml`. Unlisted tasks are excluded from listing and resolution;
missing or duplicate manifest tasks cause listing/setup to fail.

From the project root, install and list Science tasks with:

```bash
./src/server/environments/terminal_bench/host_runtime/setup.sh --benchmarks science-0.1
./src/server/environments/terminal_bench/host_runtime/list_tasks.sh \
  --benchmark terminal-bench-science-0.1 --format yaml
```

Setup writes `host_runtime/tbscience-games.generated.yaml`. Use its game IDs in
an `environment: terminal_bench` run configuration with the socket mount described
below. The project supplies `run_config_terminal_bench_science_0.1.yaml` with all 70 release tasks:

```bash
python3 run_controller_tb.py --benchmarks science-0.1 --run-config run_config_terminal_bench_science_0.1.yaml
```

The wrapper forwards `--benchmarks` to setup and checks that every configured game
belongs to the selection, with the bundled smoke test also allowed. The YAML
`games:` list determines which tasks run. Omitting `--benchmarks` retains TB2.1 setup.

`TBSCIENCE_ROOT` changes the checkout location; direct Python CLI calls accept
`--tbscience-root`. `--tbscience-ref` overrides the initial release selection and
is recorded in the checkout lock. Setup without arguments continues to install
only TB2.1; `--benchmarks all` includes Science.

## Files from the agent workspace

An agent can create a solution locally and send it into the task sandbox:

```json
{"id":"send_path","path_local":"solution","path_sandbox":"/app/solution"}
```

`path_local` is relative to the agent workspace, independent of its current
directory. The runtime currently expects that workspace on the host at
`run/<alias>/run/agent_run/`, mounted by the bundled agents as
`/home/user/run/agent_run/`. New agents must expose this location or adapt the
integration. The agent and runtime must share the host filesystem.

Sources must be regular files or directories, without absolute paths, `..`,
symlinks, or special files. Keep them unchanged until the action returns. For a
file, `path_sandbox` is an absolute destination filename; for a directory, its
contents are merged into that destination directory. Its parent must exist.
Matching files are overwritten and ordinary permission bits are preserved.
Destination permissions still apply; a failed directory copy may be partial.

## Submission and results

`finish` immediately returns a terminal observation. Grading continues on the
host, and verifier scores and diagnostics are not returned to the agent. If an
agent exits without `finish`, closing the server submits the prepared task's
current state. A task that never completed preparation has no prepared state to
grade and is recorded as an error or aborted attempt.

The controller can finish while grading is still active. The Terminal-Bench
wrapper waits for the runtime to finish; with manual startup, let the runtime
drain before archiving results. Background grading can overlap later tasks.

Prepared tasks have no additional supervisor finalization deadline. Harbor's
native verifier timeout still applies. The former `--finish-grace-seconds` option
has been removed; `--cleanup-grace-seconds` only bounds cleanup of preparation
that never produced a usable task. A stuck operation in a prepared worker can
therefore keep runtime shutdown waiting until it is manually terminated.

Private outputs live in `run_tb/results/<alias>/`, including `result.json`,
runtime logs, and the Harbor trial. The same alias identifies the agent and
server files under `run/<alias>/`. See the
[analysis guide](../../../../analysis_scripts/README.md#terminal-bench) for reports.

## Configuration and implementation

Select `environment: terminal_bench` and use the versioned task IDs listed by the
host runtime. The supplied configurations mount `run_tb/socket/` into the server
at `/run/terminal-bench/`. Only the socket directory is mounted there; task
checkouts and private results remain on the host.

The adapter accepts `socket_path` and `request_timeout_seconds` in
`environment_parameters`. Defaults are `/run/terminal-bench/runtime.sock` and
`null` (no manager-to-runtime socket timeout). Omitting `request_timeout_seconds`
or setting it to `null` waits for completion or a connection failure. A finite,
positive number such as `36000` enables an explicit socket timeout. The runner
supplies each task's alias as `session_id`; do not set a shared session ID in the
configuration. These settings do not change Harbor's task time budgets or the
short timeout for acknowledging cleanup.

The four bundled agent variants accept top-level `http_read_timeout_seconds`
and `supervisor_inactivity_timeout_seconds` run-config settings. Their defaults
are 3900 seconds (65 minutes) and 7200 seconds (two hours). The Science configuration
selects `agent_v1_07_wstop2` and raises these to 36300 and 39600 seconds. The
supervisor measures time without `agent.log` updates, including initialization;
Harbor continues to enforce the task's own solving and verifier budgets.

An HTTP timeout does not cancel the server's wait for the runtime; a stalled
connection may require process termination when the manager's socket timeout is
disabled.

The manager in [environment_manager.py](environment_manager.py) owns one runtime
session. It validates configuration before startup and delegates preparation,
actions, and cleanup to the host. Irrecoverable connection or worker failures
end that session; state-changing actions are not automatically replayed.

See [host_runtime/](host_runtime/) for implementation and
[UPSTREAM.md](host_runtime/UPSTREAM.md) for dataset sources. Run adapter tests
without Harbor or Docker from `envtraverse_v0.1/`:

```bash
PYTHON=python3 ./src/server/environments/terminal_bench/host_runtime/test.sh
```

After host setup, omit `PYTHON=python3` to also exercise the installed Harbor
contract. Live Docker tests are opt-in through `TB_RUN_DOCKER_SMOKE=1` and
`TB_RUN_DOCKER_TIMEOUT=1`; they use synthetic tasks and make no model calls.
