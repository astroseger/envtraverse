# OpenCode agent v1.08 wstop3

This package ports the prompts, environment client, continuation loop, stuck
handling, and recovery supervisor from `agent_v1_08_wstop3` to OpenCode. It uses
the separate `run_controller_oc.py` and `run_funs_oc.py` entry points. The image
pins `@opencode/cli@2.3.0` and retains the original scientific Python packages.

## Run

From the repository root, edit `run_config_opencode.yaml`, then run:

```bash
python3 run_controller_oc.py
```

The supplied configuration runs the example `counter` environment using
`oc_rod1_1` and `openai/gpt-5.6-sol` with the `max` variant. The controller builds
the selected images, checks each account with a short live model request, and
schedules games across the configured accounts. Docker must be available to the
current user. Environment-specific host variables and mounts work as in the
original controller.

Outputs go under `run/`. That directory must not already exist. To use a fresh
output location with the original account directory:

```bash
python3 run_controller_oc.py \
  --run-root /tmp/opencode-experiment \
  --run-config "$PWD/run_config_opencode.yaml" \
  --accounts-dir "$PWD/opencode_chatgpt_accounts"
```

Without explicit overrides, configuration and accounts are resolved relative to
`--run-root`: `run_config_opencode.yaml` and `opencode_chatgpt_accounts/`.

## Accounts and sessions

Every selected account directory is mounted directly and read/write at
`/home/user/.local/share/opencode`. Credentials are never copied or exported by
this controller. Token refreshes and the OpenCode database remain in the
original directory. Account locks prevent concurrent controller runs from
sharing a selected account; the scheduler uses each account for one game at a
time.

Each game starts a fresh OpenCode session. The controller does not automatically
clean accounts between games: the database retains sessions and conversation
history, including the preflight account check. Cleaning an account separately
also removes the session history required to recover an existing game.

The game workspace stores its session ID atomically in `.opencode_session_id`.
Recovery starts a fresh session when the environment is still at step zero,
resumes the saved session after progress, and avoids model calls when the
environment is already terminal. A missing saved session after progress fails
instead of silently starting a replacement conversation. The supervisor retains
the original inactivity and retry behavior and stops active child processes on
interruption.

The inherited supervisor returns exit code zero after exhausting recovery
attempts, even if the environment is unfinished. Assess completion from the
saved environment state and results, together with the supervisor log.

## Configuration

The generated OpenCode system configuration is mounted read-only at
`/etc/opencode/opencode.json`. It allows all OpenCode permissions and sets
`websearch: false`; the runner also passes `--auto`. This removes OpenCode
permission prompts as far as practical. The Docker container boundary and the
controller's existing resource and proxy/network restrictions remain in effect.

OpenCode's native tools, subagents, compaction, and shell behavior retain their
defaults. No timeout plugin or custom tool implementation is installed.

| Run configuration key | Default | Effect |
| --- | --- | --- |
| `model` | Omitted | Optional native model selection, such as `openai/gpt-5.6-sol`. A native `#variant` suffix is also accepted. |
| `reasoning_effort` | Omitted | Native model variant, passed as `model#variant`. Requires an explicit `model`; do not combine with a variant already in `model`. Supported values depend on the selected model. |
| `opencode_thinking` | `false` | Include model reasoning in the emitted output/logs; does not select reasoning effort. |
| `opencode_agent` | Omitted | Select a native OpenCode agent with `--agent`. |
| `opencode_shell_timeout_background` | Omitted | Set the native experimental `shell_timeout_background` boolean. When omitted, no override is written. This is not a numeric shell timeout. |
| `http_read_timeout_seconds` | `3900` | Environment client HTTP read timeout. |
| `supervisor_inactivity_timeout_seconds` | `7200` | Stop and recover an agent after this many seconds without progress-log updates. |
| `recovery_delay_seconds` | `0` | Delay before recovery attempts. |
| `max_recovery_attempts` | `10` | Maximum supervisor recovery attempts; `0` disables retries. |

The last four settings belong to the environment harness, independently of
OpenCode's native shell timeout. The sample explicitly selects a model and
variant; removing both preserves OpenCode's own model selection behavior.

## Logs and usage

For each game alias, `run/<alias>/run/` contains:

- `agent.log`: harness messages and model progress events used by the supervisor.
- `agent_runner.log`: supervisor decisions and recovery attempts.
- `opencode_events.jsonl`: complete raw JSON output from OpenCode turns, including
  lifecycle events that do not count as supervisor progress.
- `opencode_stderr.log`: OpenCode diagnostics.
- `.opencode_session_id`: the session to resume after environment progress.
- `agent_run/`: the working directory and its snapshots.

The controller log is `run/runner.log`; environment state and observations live
beside each game's `run/` directory. The existing token extraction analysis
recognizes OpenCode `step_finish` events. These report emitted root-session
steps and do not include background subagent usage; they are not a complete
account-level usage total.
