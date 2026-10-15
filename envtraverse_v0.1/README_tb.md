# Terminal-Bench

The `terminal_bench` environment connects each game server to a host runtime
through one shared Unix socket. The runtime runs one worker process and Harbor
trial per game. Harbor and Docker run on the host; the environment server needs
only the lightweight socket client.

## Setup

Requirements: Linux, Python 3.12+, Git, Docker Engine and Docker Compose v2.
Run the runtime as the same host user used to build the server images, so the
server UID can access the private socket.

```bash
./src/server/environments/terminal_bench/host_runtime/setup.sh
```

This installs `harbor==0.23.0` into `host_runtime/.venv/` and downloads **only
Terminal-Bench 2.1** into `host_runtime/.tb-data/`. These reusable setup files are
separate from experiment output. The downloaded revision is pinned in a lock file;
repeated setup reuses it. Task Docker images are prepared when a task starts.
Neither the virtualenv nor downloaded datasets enter the server Docker image.

List available game IDs:

```bash
./src/server/environments/terminal_bench/host_runtime/list_tasks.sh \
  --benchmark terminal-bench-2.1 --format yaml
```

Optional datasets remain available with `setup.sh --benchmarks 3.0,4.0` or
`--benchmarks all`. Setup also accepts `--tb21-ref`, `--tb3-ref`, and `--tb4-ref`
for an initial explicit revision. Existing pins are not silently updated.
`TB_VENV`, `TB21_ROOT`, `TB3_ROOT`, and `TB4_ROOT` can select existing absolute
locations. `BOOTSTRAP_PYTHON` selects the Python used to create the virtualenv.

## Run an experiment

Archive any previous `run/` and `run_tb/` directories first. The host script
refuses to start if either name already exists, including a dangling symlink.
It does not delete or reuse previous experiment output.

Run setup, the host runtime, and the controller with one command:

```bash
python3 run_controller_tb.py --run-config run_config_terminal_bench.yaml
```

With no arguments, the wrapper uses the project's `run_config.yaml`. It runs
`setup.sh` with its default Terminal-Bench 2.1 setup, waits for `Runtime ready`,
and starts `run_controller.py`. Runtime output appears live with a `[runtime]`
prefix and is also saved to `run_tb/runtime.log`.

After the controller exits, the wrapper requests graceful runtime shutdown and
waits for verification and worker cleanup before exiting. It also does this when
the controller fails. Ctrl+C stops the controller first, then drains the runtime;
it may take time for verification to finish. The wrapper preserves the controller's
exit code, reports runtime failures as errors, and returns 130 when interrupted.
Task rewards and verifier errors remain available in the host results.

To manage the two processes manually instead, start the runtime in one terminal:

```bash
./src/server/environments/terminal_bench/host_runtime/run.sh
```

Wait for `Runtime ready`. The script works from any current directory and creates
all experiment output under the project root's `run_tb/`. It binds with a short
relative pathname, so long checkout paths do not exceed the Linux Unix-socket
pathname limit. Leave this terminal running.

In another terminal:

```bash
python3 run_controller.py --run-config run_config_terminal_bench.yaml
```

The example configuration is:

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

`codex_account_fast_service_tier` is an optional boolean, defaulting to `false`.
When enabled, `run_controller.py` writes `service_tier = "fast"` to the account's
`config.toml` after each cleanup and before starting the agent. Both Terminal-Bench
example configs enable it.

`server_mounts` is available in both run controllers. Each entry is a directory
bind mount with exactly `source` and `target`. Relative sources resolve against
the project root, independently of the config file's location or shell CWD.
Sources must already exist. Targets are absolute container paths; `/data` is
reserved for the game's server output. The socket directory is mounted only
into servers, not agents. No additional controller network or TCP port is needed.

The manager defaults to `/run/terminal-bench/runtime.sock`. For a different
container location set `environment_parameters.socket_path`. Each controller
assigns the game's generated UUID alias as its runtime `session_id`; do not set
a shared `session_id` in the YAML.

## Upload files from the agent workspace

`send_path` copies a regular file or directory from the agent's shared workspace
into the task container:

```json
{"id":"send_path","path_local":"solution","path_sandbox":"/app/solution"}
```

`path_local` is relative to the workspace root, regardless of the caller's current
directory. For `agent_v1`, that is `/home/user/run/agent_run` in the agent container
and `run/<alias>/run/agent_run` on the host. Future agents must expose their upload
workspace at the same host location. Container-only files outside this workspace
must be copied into it first. The runtime and agent must share this host filesystem.

`run.sh` passes the project's `run/` location using `--runs-dir`; the runtime does
not create it. Each worker derives its own workspace from the controller-assigned
session ID. Client, server, and Unix-socket messages carry only the relative source
and sandbox destination. File bytes never enter action logs or the socket protocol.

Absolute source paths, `..`, symlinks (including nested ones), and special files
are rejected. `send_path` has no file or directory size limit; the separate
`get_file` limit still applies to downloads. Sources must stay unchanged until
the action returns. The worker makes a local snapshot using file descriptors that
do not follow symlinks, then uploads it using Harbor. Temporary host snapshots
stay under `run_tb/results/<alias>/uploads/` and are removed after the action. All of this happens during the normal solving
phase and inside that session's worker.

The destination is an absolute filename for a file, or the directory receiving a
source directory's contents. Its parent must exist. Matching files are overwritten,
unrelated files remain, and ordinary permission bits (including executable bits)
are preserved. Harbor uploads into temporary sandbox staging; the adapter assigns
that staging to the actual task user and performs the final copy as that user.
This also handles a Dockerfile `USER` when `task.toml` does not specify one.
Destination permissions still apply, and a failed directory copy can leave partial
changes. Temporary staging is cleaned after success or failure.

## Submission and grading

`finish` immediately makes the game terminal. The host acknowledges submission
without waiting for the worker to grade it. The agent receives a terminal message,
never verifier scores or diagnostic results.

If an agent exits without `finish`, server shutdown submits the prepared task's
current state for grading. Native solving timeouts also end solving and preserve
Harbor's verification. A task that failed or was cancelled before preparation
could complete is recorded as an error or aborted attempt; it has no usable
prepared state to grade.

Server shutdown only waits briefly for the runtime to acknowledge the close
request. Grading, cleanup, and worker reaping continue independently on the host.
The controller can finish before validation does. Read the final benchmark
outcomes from the host results, not from the controller exit status.

The runtime prints a completion summary after each finalized session, for example:

```text
2 tasks validated. Errors: 0. Aborted: 0. Active: 0. No active sessions.
```

A valid reward of zero counts as validated. Active includes preparing, solving,
validation and cleanup. The runtime remains running when idle: the controller may
submit another game. Once the controller has finished and no sessions remain
active, stop the runtime with Ctrl+C. Ctrl+C while prepared tasks remain submits
and drains them before exiting.

Background validation can overlap later games, so the number of Harbor containers
can exceed the number of active agent slots. Choose concurrency for the host's
available CPU, RAM and disk capacity.

## Output

```text
run/<alias>/server/server.log
run_tb/
  runtime.log
  socket/runtime.sock
  results/<alias>/
    session.json
    lifecycle.json
    result.json
    runtime.log
    events.jsonl
    task-files.json
    worker-spec.json
    trial/<alias>/...
```

The same alias connects controller logs, server logs, and host results. Task names,
source revisions, task-file hashes, rewards, and verifier details stay in host
records. `result.json` distinguishes `status`, `termination_reason`,
`verification_status`, and `rewards`. Finished validation does not imply a solved
task. An expired solving limit can still receive a positive verifier reward.

Socket access uses a private directory and a 0600 socket. Only the socket directory
is mounted into the server; task checkouts and private results stay on the host.
Docker's own image/cache storage remains managed by Docker.

See the [analysis guide](analysis_scripts/README.md#terminal-bench) for reports
from the completed experiment.

## Time limits and failure handling

Native **solving and verifier limits are preserved**. Thinking and pauses between
actions count toward Harbor's solving clock; preparation and grading do not.
The environment description states the limit as
`Solving time budget: <seconds> seconds (wall-clock).` Observations do not include
a countdown or timing file. Detailed timing remains in private host records, so
it can be exposed again in a future experiment without changing timeout
enforcement. The adapter's agent-facing text and observation metadata omit
backend names and scoring details; task instructions are preserved.
The neutral `trial/` output directory also avoids exposing the runtime name through
task-container mount metadata.

Infrastructure builds receive at least one hour, without multiplying the solving
or verifier budgets. The manager's socket request timeout defaults to one hour;
the existing HTTP client allows 65 minutes and the agent watchdog allows two hours.
Manager close acknowledgement has a five-second timeout. Host finalization has a
two-hour grace; cancelling incomplete preparation has a two-minute cleanup grace.
These are transport/cleanup bounds, not extra solving time.

Irrecoverable worker or socket loss terminates the affected game. State-changing
actions are never automatically replayed. Host death or forced worker termination
can require manual cleanup of that trial's Docker resources; affected results
record errors and `cleanup_required` where detected. There is no automatic
resumption of a crashed runtime or reuse of an old experiment directory.

## Tests

Run the adapter tests without Harbor or Docker:

```bash
PYTHON=python3 ./src/server/environments/terminal_bench/host_runtime/test.sh
```

With host setup complete, include the installed-Harbor contract test:

```bash
./src/server/environments/terminal_bench/host_runtime/test.sh
```

Opt in to real Docker tests using the bundled synthetic fixture:

```bash
TB_RUN_DOCKER_SMOKE=1 TB_RUN_DOCKER_TIMEOUT=1 \
  ./src/server/environments/terminal_bench/host_runtime/test.sh
```

These exercise terminal actions, workspace uploads through the actual HTTP client
and server, root/non-root file ownership, directory permissions, background
submission, early-stop grading, and native timeout verification. The upload tests
use `python3` for the HTTP server/client (Flask and Requests required); set
`TB_SERVER_PYTHON` to select another interpreter. Synthetic timeout tests alter
only temporary copies of the bundled test fixture, not downloaded benchmark tasks. No model calls or
account credentials are used.

The integration is based on the supplied `terminal_bench_pair` 0.31 adapter.
The local protocol is version 3 because finish/close acknowledgements now precede
validation. Host and server use the same source; rebuild server images after
changes (the normal controllers build them automatically).
