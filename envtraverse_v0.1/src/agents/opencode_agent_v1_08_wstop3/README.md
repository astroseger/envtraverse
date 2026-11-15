# OpenCode agent v1.08 wstop3

This package ports the prompts, environment client, continuation loop, stuck
handling, and recovery supervisor from `agent_v1_08_wstop3` to OpenCode. It uses
the shared `run_controller.py` with `backend: opencode_chatgpt_accounts`. The image
pins `@opencode/cli@2.3.0` and retains the original scientific Python packages.

## Run

From the repository root, edit `run_config_opencode.yaml`, then run:

```bash
python3 run_controller.py --run-config run_config_opencode.yaml
```

The supplied configuration runs the example `counter` environment using
`oc_rod1_1` and `openai/gpt-6.1-terra` with the `max` variant. The controller builds
the selected images, checks each account with a short live model request, and
schedules games across the configured accounts. Docker must be available to the
current user. Environment-specific host variables and mounts work as in the
original controller.

Outputs go under `run/`. That directory must not already exist. To use a fresh
output location with the original account directory:

```bash
python3 run_controller.py \
  --run-root /tmp/opencode-experiment \
  --run-config "$PWD/run_config_opencode.yaml" \
  --accounts-dir "$PWD/opencode_chatgpt_accounts"
```

Accounts default to `opencode_chatgpt_accounts/` under `--run-root` when the
configuration selects `backend: opencode_chatgpt_accounts`. The controller
defaults to `run_config.yaml`; pass `--run-config run_config_opencode.yaml` to
select the supplied OpenCode configuration. Configure account names with
`accounts: [...]`; the older `opencode_accounts` key is rejected. Each account runs one game
at a time; `max_parallel_games` is only available for the API-key backend.

## Accounts and sessions

Every selected account directory is mounted directly and read/write at
`/home/user/.local/share/opencode`. Online requests always use this same account
path, so refreshed credentials persist there. Account locks prevent concurrent
controller runs from sharing a selected account; the scheduler uses each account
for one game at a time.

Before online use, the controller also checks for active Docker containers
mounting the account, including containers left by a previous controller.

Before each fresh game, the controller calls `clean_account.sh` with its existing
account lock and built agent image. Cleanup exports credentials temporarily,
imports them into a fresh database, and verifies the exported fields, all with
networking disabled. It then replaces the data at the selected account path and
deletes the old state before starting the online game. This removes previous
conversations, including the preflight account check, while preserving current
credentials. A failed cleanup prevents that game from starting.

Temporary credential exports are used only for offline cleanup. The controller
does not create online account clones or restore stale tokens over refreshed
credentials. Each game starts a fresh OpenCode session; recovery within that
game keeps the same account database and does not clean it. Starting the next
fresh game removes the session history needed to resume the previous game.

After a game's container stops, the controller collects its recorded token usage
offline into `run/<alias>/run/opencode_usage.json` before allowing account
cleanup. The snapshot contains usage counters and session identifiers, not
credentials or conversation contents. A failed collection blocks cleanup so the
database counters remain available.

A sibling `.<account>.opencode.pending-usage.json` marker tracks an outstanding
collection. Account commands and controller preflight refuse accounts with this
marker, including after an interrupted or crashed controller. To recover from
the repository root, run `python3 opencode_usage.py ACCOUNT`, optionally passing
`--image SAME_VERSION_IMAGE`. Recovery verifies that the previous game container
has stopped, saves the usage snapshot, and clears the marker only after success.

The game workspace stores its session ID atomically in `.opencode_session_id`.
Recovery starts a fresh session when the environment is still at step zero,
resumes the saved session after progress, and avoids model calls when the
environment is already terminal. A missing saved session after progress fails
instead of silently starting a replacement conversation. The supervisor retains
the original inactivity and retry behavior and stops active child processes on
interruption.

Before resuming, the session-existence lookup allows up to 600 seconds, including
CLI startup. It returns as soon as the lookup completes; this is not a fixed
wait. Reported errors fail immediately and trigger the normal recovery behavior.

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

All settings in the table below belong inside the `agent_parameters` mapping.
OpenCode configurations must use this nested form; legacy root settings are
supported only for the Codex ChatGPT backend.

| Run configuration key | Default | Effect |
| --- | --- | --- |
| `model` | Required | Explicit model selection, such as `openai/gpt-6.1-terra`. The `openai/` prefix may be omitted. Do not include a `#variant` suffix. |
| `reasoning_effort` | Required | Explicit native model variant, such as `max`, passed as `model#variant`. Supported values depend on the selected model; do not include `#` or `/`. |
| `opencode_thinking` | `false` | Include model reasoning in the emitted output/logs; does not select reasoning effort. |
| `opencode_agent` | Omitted | Select a native OpenCode agent with `--agent`. |
| `opencode_shell_timeout_background` | Omitted | Set the native experimental `shell_timeout_background` boolean. When omitted, no override is written. This is not a numeric shell timeout. |
| `http_read_timeout_seconds` | `3900` | Environment client HTTP read timeout. |
| `supervisor_inactivity_timeout_seconds` | `7200` | Stop and recover an agent after this many seconds without `agent.log` updates. Every OpenCode stdout line refreshes this watchdog. |
| `recovery_delay_seconds` | `0` | Delay before recovery attempts. |
| `max_recovery_attempts` | `10` | Maximum supervisor recovery attempts; `0` disables retries. |

Both `model` and `reasoning_effort` must be non-empty strings without whitespace
inside `agent_parameters`. Missing or null values are rejected. Specify them as
separate fields; the sample selects `openai/gpt-6.1-terra` and `max`.

The last four settings belong to the environment harness, independently of
OpenCode's native shell timeout.

## Logs and usage

For each game alias, `run/<alias>/run/` contains:

- `agent.log`: harness messages and all OpenCode stdout, including retries,
  `background_wait` events, and non-JSON diagnostics. Every stdout line refreshes
  the supervisor's inactivity watchdog.
- `agent_runner.log`: supervisor decisions and recovery attempts.
- `opencode_events.jsonl`: a separate raw copy of OpenCode stdout, including any
  non-JSON lines.
- `opencode_stderr.log`: separate OpenCode stderr; these writes do not refresh
  the inactivity watchdog.
- `opencode_usage.json`: validated counters collected offline from the account
  after the game stops, before the database can be cleaned.
- `.opencode_session_id`: the session to resume after environment progress.
- `agent_run/`: the working directory and its snapshots.

The controller log is `run/runner.log`; environment state and observations live
beside each game's `run/` directory. Token extraction prefers the recorded usage
snapshot, even when `agent.log` is missing, and never adds stdout counters to it.
The snapshot includes all persisted session counters: root and child sessions,
recovery sessions, title generation, compaction, and known failed-step usage.
It cannot recover usage the provider did not report or OpenCode did not persist.

A malformed or unsupported snapshot stops analysis instead of silently falling
back. For older runs without a snapshot, analysis still reads `step_finish`
events from stdout and warns that this root-only usage is incomplete. The output
keeps the existing input, cached-input, and output token fields and does not
estimate subscription billing or cost.
