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

Select the agent-facing interface in the run configuration:

```yaml
environment: terminal_bench
environment_parameters:
  environment_version: "v1.2"
```

The supported values are `v1` and `v1.2`. Omitting `environment_version` keeps
the original `v1` behavior. Unknown values fail at startup. This selection is
fixed per session, so the same runtime can serve both versions concurrently.
It is independent of the benchmark dataset version, runtime package release,
and socket protocol version. The runtime records it in session and result metadata.

Each version exposes six actions:

| Action | Purpose |
| --- | --- |
| `terminal_send` | Send text or named keys to an interactive terminal |
| `terminal_read` | Read a terminal screen or recent history |
| `terminal_new` | Open another terminal in the same task container |
| `get_file` (`v1`) | Retrieve a file, at most 32 MiB, as `file.bin` and `file.json` observation artifacts |
| `get_path` (`v1.2`) | Copy a file or directory from the task container into the agent workspace |
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
`<run-root>/run/<alias>/run/agent_run/`, mounted by the bundled agents as
`/home/user/run/agent_run/`. New agents must expose this location or adapt the
integration. The agent and runtime must share the host filesystem.

Sources must be regular files or directories, without absolute paths, `..`,
symlinks, or special files. Keep them unchanged until the action returns. For a
file, `path_sandbox` is an absolute destination filename; for a directory, its
contents are merged into that destination directory. Its parent must exist.
Matching files are overwritten and ordinary permission bits are preserved.
Destination permissions still apply; a failed directory copy may be partial.

## Files into the agent workspace

Interface `v1.2` replaces `get_file` with the reverse transfer:

```json
{"id":"get_path","path_sandbox":"/app/results","path_local":"results"}
```

This writes to `/home/user/run/agent_run/results`, independent of the agent's
current directory. `path_sandbox` is an absolute path in the main task container,
even if a terminal is connected elsewhere through SSH. A file copies to the exact
local filename; a directory's contents merge into the exact local directory,
creating it if needed. The parent must already exist, and matching files are
overwritten. `path_local: "."` merges a directory into the workspace root without
changing the root's permissions.

The source must be readable by the task user and contain only regular files and
directories. Source symlinks and special files are rejected, including symlinks
in source ancestors. Absolute local paths, `..`, and destination symlinks are
also rejected. Ordinary permission bits, including executable bits, are preserved;
ownership belongs to the host runtime user, matching the bundled agent's user.
Destination permissions still apply. Individual files are replaced atomically;
a directory merge stops at the first failure and may be partial.

The action returns a compact `transfer.json` receipt:

```json
{
  "status": "success",
  "path_sandbox": "/app/results",
  "path_local": "results",
  "kind": "directory",
  "files_copied": 5,
  "bytes_copied": 12000
}
```

If copying fails after making workspace changes, the action still returns a
normal observation and advances the step, with a partial receipt:

```json
{
  "status": "partial",
  "path_sandbox": "/app/results",
  "path_local": "results",
  "kind": "directory",
  "files_copied": 2,
  "files_not_copied": 3,
  "bytes_copied": 4800,
  "error": "Could not copy results/c.txt: permission denied."
}
```

Counts include only completed file copies, including overwrites and empty files.
`files_not_copied` includes both the failed and unattempted files from the staged
source. Success receipts omit `files_not_copied` and `error`; neither receipt
lists individual files. Creating a directory or changing its permissions also
counts as progress, even with zero files copied. A later directory-permission
failure can therefore return `partial` with `files_not_copied: 0`. Failures
before any workspace changes remain action errors. A successful empty-directory
merge returns `success`, including when nothing needed changing. Cancellation,
timeouts, or a lost connection can still prevent receipt delivery.

File contents go directly into the shared workspace, so
the old 32 MiB retrieval limit does not apply. Transfers still use disk space and
the task's solving time. The export uses GNU tar and find in the task container,
executed as the task user; Docker only downloads the staged archive. Host-side
archive validation rejects links and traversal before any workspace writes.

Interface `v1` retains its original action schemas, description, size limit and
observation contents. It does not accept `get_path`; `v1.2` does not accept
`get_file`. The generic HTTP client is unchanged. Update both the host runtime
and environment-server image before selecting `v1.2`; startup refuses a runtime
that does not acknowledge the requested version.

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

Private outputs live in `<run-root>/run_tb/results/<alias>/`, including `result.json`,
runtime logs, and the Harbor trial. The same alias identifies the agent and
server files under `<run-root>/run/<alias>/`. See the
[analysis guide](../../../../analysis_scripts/README.md#terminal-bench) for reports.

## Configuration and implementation

Select `environment: terminal_bench` and use the versioned task IDs listed by the
host runtime. The supplied configurations mount `run_tb/socket/` into the server
at `/run/terminal-bench/`. Only the socket directory is mounted there; task
checkouts and private results remain on the host.

All controllers and `host_runtime/run.sh` accept `--run-root`, defaulting to the
source checkout. Relative mount sources, including `run_tb/socket/`, resolve
under that root. `run_controller_tb.py` forwards it to both the runtime and the
controller; for manual startup, supply the same root to each. Datasets and the
Harbor virtual environment retain their existing source-checkout locations.

The adapter accepts `socket_path`, `request_timeout_seconds`, and
`environment_version` in `environment_parameters`. Their defaults are
`/run/terminal-bench/runtime.sock`, `null` (no manager-to-runtime socket timeout),
and `v1`. Omitting `request_timeout_seconds`
or setting it to `null` waits for completion or a connection failure. A finite,
positive number such as `36000` enables an explicit socket timeout. The runner
supplies each task's alias as `session_id`; do not set a shared session ID in the
configuration. These settings do not change Harbor's task time budgets or the
short timeout for acknowledging cleanup.

The four bundled agent variants accept top-level `http_read_timeout_seconds`
and `supervisor_inactivity_timeout_seconds` run-config settings. Their defaults
are 3900 seconds (65 minutes) and 7200 seconds (two hours). The Science configuration
selects `agent_v1_08_wstop3` and raises these to 36300 and 39600 seconds. The
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
