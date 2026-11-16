# OpenCode baseline agent v1

This package runs the `agent_v1` baseline through OpenCode, using the shared
controller with `backend: opencode_chatgpt_accounts`. Its Docker image defaults
to `@opencode/cli@2.3.0` and includes the scientific Python packages used by the
Codex agents.

The baseline stops when the environment is terminal or after one ineffective
stuck reminder. When the environment step stops advancing, it sends the normal
continuation prompt, including the continuation string, followed by the stuck
reminder. If the step is still unchanged on the next inspection, the agent
stops. If progress resumes, normal continuation resumes too.

## Run

From the repository root, copy the
[OpenCode example](../../../run_config_examples/run_config_opencode.yaml):

```bash
cp run_config_examples/run_config_opencode.yaml run_config.yaml
```

The example selects `opencode_agent_v1_08_wstop3`. Edit your copy to select the
baseline:

```yaml
backend: opencode_chatgpt_accounts
agent: opencode_agent_v1
```

Set `accounts` to your prepared account names and choose
`agent_parameters.model` and `agent_parameters.reasoning_effort`. Both are
required. Keep the example environment and games, or select another bundled
environment. Then run:

```bash
python3 run_controller.py --run-config run_config.yaml
```

The controller builds the images, checks each account with a short live request,
and runs one game at a time per account. The run root defaults to the caller's working
directory; its `run/` directory must not already exist. To use a fresh run root
with the edited configuration and original account directories:

```bash
python3 run_controller.py \
  --run-root /tmp/opencode-baseline-experiment \
  --run-config "$PWD/run_config.yaml" \
  --accounts-dir "$PWD/opencode_chatgpt_accounts"
```

Without `--accounts-dir`, accounts are located under
`<run-root>/opencode_chatgpt_accounts/`. The account list uses `accounts: [...]`;
`max_parallel_games` is only available for the API-key backend.

## Accounts and recovery

Account directories are mounted directly at `/home/user/.local/share/opencode`,
so refreshed credentials stay in the original location. Account locks and Docker
mount checks prevent concurrent reuse. Before every fresh game, offline cleanup
preserves credentials and clears previous conversation history, including the
preflight check. Cleanup failure prevents that game from starting.

Each game starts a fresh session and saves its ID in `.opencode_session_id`.
Recovery within a game keeps the database: it starts fresh at environment step
zero, resumes the saved session after progress, and avoids model calls once the
environment is terminal. A missing saved session after progress fails instead of
starting a replacement conversation. The supervisor can return exit code zero
after exhausting retries; assess completion from saved environment state and
results, together with `agent_runner.log`.

After the game stops, the controller saves recorded account-wide usage before
allowing another cleanup. A pending-usage marker blocks account reuse if this
collection fails or is interrupted. See the
[account guide](../../../opencode_chatgpt_accounts/README.md) for preparation,
cleanup, and recovery instructions.

## Configuration

Runtime settings belong in `agent_parameters`. Model names may omit `openai/`;
provide reasoning effort separately, without a `#variant` suffix in the model.
OpenCode combines them into its native `model#variant` reference.

| Parameter | Default | Effect |
| --- | --- | --- |
| `model` | Required | OpenAI model, such as `openai/gpt-6.1-terra`. |
| `reasoning_effort` | Required | Native variant supported by that model, such as `max`. |
| `opencode_thinking` | `false` | Include model reasoning in output and logs. |
| `opencode_agent` | Omitted | Select a native OpenCode agent with `--agent`. |
| `opencode_shell_timeout_background` | Omitted | Override the native experimental boolean; this is not a numeric timeout. |
| `http_read_timeout_seconds` | `3900` | Environment client HTTP read timeout. |
| `supervisor_inactivity_timeout_seconds` | `7200` | Recover after this many seconds without `agent.log` updates. |
| `recovery_delay_seconds` | `0` | Delay before recovery attempts. |
| `max_recovery_attempts` | `10` | Maximum recovery attempts; `0` disables retries. |

The generated OpenCode configuration allows all permissions and disables
`websearch`; execution also uses `--auto`. Docker isolation and the controller's
resource and network restrictions still apply. Native tools, subagents,
compaction, and shell behavior otherwise retain their defaults. No custom
timeout plugin is installed.

## Logs and usage

Each game's `run/<alias>/run/` contains `agent.log`, `agent_runner.log`, raw
OpenCode stdout in `opencode_events.jsonl`, separate `opencode_stderr.log`, the
saved `.opencode_session_id`, and the `agent_run/` workspace. Every stdout line
refreshes the inactivity watchdog; stderr alone does not.

After shutdown, `opencode_usage.json` records counters for all persisted sessions,
including subagents and recovery sessions. It contains session identifiers and
usage counters, not credentials or conversations. Analysis prefers this snapshot
over stdout counters. It cannot recover usage that OpenCode did not persist.
