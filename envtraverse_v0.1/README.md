# EnvTraverse

Each active game runs in its own server container and agent container. Both run
controllers share two Docker networks and one proxy across the experiment.
Direct server launch instructions are in [the server README](src/server/README.md).

## Introduction

This repository contains the source code for the BASELINE1 agent and the Docker-based automation used to run it.

If you want to check that the agent does not have access to game-specific hidden information and is intended to be general within the ARC-AGI-3 universe, read [AGENT.md](AGENT.md).

To run the system you need:

- a Linux machine with Docker installed;
- any credentials required by the selected environment (for ARC3, `ARC_API_KEY`);
- a ChatGPT Pro subscription.

With the default GPT-5.5/medium configuration, one ChatGPT Pro subscription (200 USD) is enough to run a full experiment for roughly 2-8 games within the Codex weekly limit for that account.

The supervisor attempts recovery after an agent exits or its `agent.log` has no
new output for two hours. Recovery is limited to 10 attempts and cannot resolve
persistent account or service limits. Known failure cases include:

- Codex can occasionally crash with a "model at model capacity limits" error. In my experience, this happened about twice in a month.
- A run can stop when the Codex account reaches its 5-hour or weekly usage limit. In my experience, with a single ChatGPT Pro subscription (200 USD), you can safely run two games in parallel without hitting the 5-hour limit.

## System Requirements

For one game, expect to need about 1-2 moderately fast CPU cores and about 3 GB of memory.

For 25 games in parallel, a reasonable target is about 48 CPU cores and 64 GB RAM. For two games in parallel, almost any modern machine should be enough.

## How to Run the System

### Set up Codex accounts

As mentioned above, you can run two games in parallel with a single ChatGPT Pro subscription (200 USD) without hitting the 5-hour usage limit. To do this, set up two Codex account folders authenticated with the same ChatGPT subscription.

To create and authenticate those accounts:

1. Optionally build the shared account-tools image ahead of time:

```bash
./codex_accounts/build_docker.sh --if-missing
```

This builds `codex-accounts-image-unified-agent-v1-0`, independently of experiment
images and environments. Account scripts (including controller account checks)
automatically build it if missing, so this step is optional. Existing images are
reused unchanged, and concurrent builds are serialized across experiment folders.
Its Codex version can be updated independently of the experiment's version.
The build sends only its Dockerfile to Docker, never account directories or
credentials. Run this script without arguments when you want to rebuild the
account image manually.

2. Set up the first Codex account:

```bash
cd codex_accounts
./prepare_account.sh codex01
```

Follow the device-auth instructions and authenticate with your ChatGPT Pro subscription.

3. Optionally check the first account:

```bash
cd codex_accounts
./check_account.sh codex01
```

4. Optionally set up a second account for parallel runs:

```bash
cd codex_accounts
./prepare_account.sh codex02
```

5. Optionally check the second account:

```bash
cd codex_accounts
./check_account.sh codex02
```

After this, the system is ready to run experiments.

### Run experiments

1. Edit `run_config.yaml`.

   Select the environment package and its parameters. The active `run_config.yaml`
   currently runs the small example environment. `run_config_arc3.yaml` supplies
   one ARC3 game, `ls20`, in non-competition mode:

   ~~~yaml
   environment: arc3
   environment_parameters:
     competition: false
     tag: api-test
   environment_variables:
     - ARC_API_KEY

   games: [ls20]
   codex_accounts: [codex01]
   model: gpt-5.6-sol
   reasoning_effort: max
   ~~~

   Set `codex_accounts` to the account folders you created, if you used different folder names. `games` contains IDs understood by the selected environment. You can list more games than accounts; the controller runs up to one game per account in parallel and queues the rest.

2. Export each variable named in `environment_variables` in the controller's host environment. For the supplied ARC3 configuration:

```bash
export ARC_API_KEY=...
```

3. Start the controller, preferably inside `screen` or `tmux` because runs can take a long time:

```bash
python3 run_controller.py
```

Both controllers read the same environment settings from YAML; `--run-config PATH` selects another file. Results are written under `run/`. The controller expects `run/` not to exist before it starts.

Both controllers use the scheduler in `run_funs.py`. Account mode has one
reusable slot per account; API-key mode has one slot per game and launches all
games in parallel. Each occupied slot owns one server and one agent. The
controller prepares the game's directories, starts its server, and launches its
agent once that server is healthy. Startup checks have a 60-second deadline and
run alongside monitoring of the other pairs. Health checks stop once play begins.

When an agent exits, its server is stopped gracefully, finalizing its scorecard
if applicable. If a server exits unexpectedly, only its paired agent is stopped.
Failed starts and nonzero agent exits are logged; the slot is released and
remaining games continue. Servers and games are never automatically restarted.
The controller exits nonzero if any pair failed. A proxy failure stops the whole
experiment. Ctrl-C and SIGTERM stop all pairs, then the shared proxy and networks.
Authentication and agent Docker commands stay in their respective controllers.

Each process in an agent container has a 2 GiB data-memory limit
(`--ulimit data=2147483648:2147483648`), including Codex, its code-mode host,
and programs it launches. This limits data allocations, not total resident RAM.
The container also has a 10 GiB aggregate memory cap with no additional swap
allowance (`--memory 10g --memory-swap 10g`). Several processes can still exhaust
that aggregate cap; it does not guarantee that only the runaway process is killed.
Do not add `ulimit -v`: it limits virtual address reservations, not just RAM, and
causes Codex 0.153.4's code-mode host to crash on tool calls. The shared limits are
defined in `run_funs.py` and apply in both controllers.

`environment_parameters` defaults to `{}` and must contain JSON-compatible values with string mapping keys. It is passed unchanged to the environment manager. It is for non-secret settings, which may appear in server-side command logs. ARC3's `tag` is used only in competition mode; `competition: false` uses the SDK's normal mode, which can still require an API key and network access to download game data.

`environment_variables` defaults to `[]`. It is a list of host variable **names**, never values or `NAME=value` assignments. Listed variables must be set before startup; an empty value is forwarded unchanged, with value validation left to the environment. The controller passes them only to the server container using Docker's `-e NAME` form, not to agent containers or image build arguments. Variable values are not inserted into configuration objects, generated commands, or launcher logs. Launcher-managed `ENV_SERVER_HOST` and `ENV_SERVER_PORT` cannot be overridden through this list. Server code and Docker administrators can access these variables; they are not isolated secrets within the manager.

The controller builds the common server dependencies and the selected package's optional `requirements.txt`. For a manual build, use `./src/build_dockers.sh ENVIRONMENT`. Only that package's optional `to_agent_workspace/` helpers are copied to the agent workspace. The environment description is obtained from the manager at game startup, not copied from the server package.

Both controllers generate one UUID per experiment and build three uniquely tagged
images: `unified-agent:run-<uuid>`, `unified-server:run-<uuid>`, and
`unified-proxy:run-<uuid>`. No shared runtime tags are updated. Every container,
including queued games, uses its run's tag with `--pull=never`. Another experiment
can build or clean up its own images without replacing these tags.

Names are saved before building in `run/docker_images.json`; resolved image IDs
are logged in `run/runner.log`. `run/agent_image_id.txt` is no longer used.
After stopping the run's containers, both controllers remove only their three
image tags, without force or global pruning. Cleanup also covers partial builds,
account-check failures, startup errors, Ctrl-C, and SIGTERM. The shared account
image and other runs' tags are retained. Base images and build cache can remain
on disk to speed up later builds.

Forced termination such as SIGKILL or a machine crash cannot execute cleanup.
If needed, stop any remaining containers for that run, then remove the exact
tags recorded in its `docker_images.json` with `docker image rm --no-prune TAG...`.

The build script can also be called manually. With only the environment argument,
it generates unique tags and prints them with a cleanup command. With three
additional arguments, it uses the supplied `NAME:run-<32-hex-digit-uuid>` tags;
existing tags are rejected. Manual builds are retained for manual use and cleanup:

~~~bash
./src/build_dockers.sh arc3
~~~

For the small example environment, use `environment: example`, `games: [counter]`, `environment_parameters: {target: 3}`, and `environment_variables: []`. For Labyrinth, use `environment: agimaze_single` with a game such as `TRAINING/S0-keys/STAGE-01/0000.json` and optional parameters such as `{seed: 42}`. Neither requires an ARC API key.

For an ordered set of labyrinths, use:

~~~yaml
environment: agimaze_set_v1
games: [set01]
environment_parameters: {}
environment_variables: []
~~~

This environment reuses the single-labyrinth engine and packs. Its 233 bundled
set definitions select sequences of tutorial and hidden-map mazes, with an
explicit `{"id":"start"}` action between mazes. Parameters are the same as for
`agimaze_single`. See [Labyrinth sets](src/server/environments/agimaze_set_v1/README.md).

### Per-game files and observation protection

Both controllers prepare four host directories for each game alias:

| Host path | Container path | Contents |
| --- | --- | --- |
| `run/<alias>/client/` | `/client/` | Client code, `client_config.json`, `.client_session.json`, permanent `.client.lock` |
| `run/<alias>/observations/` | `/observations/` | Observations, `actions/`, `environment_description.md` |
| `run/<alias>/run/` | Agent: `/home/user/run/` | Agent controller, logs, writable `agent_run/` workspace |
| `run/<alias>/server/` | Server only: `/data/` | `server.log` and environment outputs, including ARC3 scorecard files |

Each server receives only its real game ID and public alias. Server files are
not mounted into the agent container. ARC3 competition runs have one scorecard
per game.

The client source lives in `src/agent/client/`, outside the initial workspace.
The agent runs `python3 /client/client.py move '<action-json>'` and reads
`/observations/environment_description.md`. Printed artifact paths are the paths
actually written, with no alternate writable view. Before launching the container,
the controller writes `/client/client_config.json` with the actual server URL and
the container's observation path, without rewriting client code:

~~~json
{"server_url": "http://<server-container-name>:8879", "observations_path": "/observations"}
~~~

The client always reads this file beside its script, not from the working
directory or environment variables. Missing or invalid configuration is an
explicit error; there is no localhost fallback. `ENV_SERVER_URL`,
`ENV_OBSERVATIONS_DIR`, and `ENV_CLIENT_DIR` no longer configure the client/agent.
Every command, including recovery, uses the server URL from `client_config.json`.
The session file stores only `token`, `step_index`, `terminal`, and
`observations_path` (for the observation-folder consistency check), not a second
copy of the server URL. The client's HTTP session ignores environment proxy and
authentication settings (`trust_env = False`); the agent's own proxy settings and
the server's environment-variable allowlist are unchanged.

Client/observation directories are `0555`, and client code, configuration, session state,
descriptions, actions, and observation files are `0444`. Before each local write,
the client takes the session lock and temporarily enables owner writes on only
the containing directory. It publishes read-only files atomically and restores
directory permissions in `finally`. The directories stay read-only during
network requests. A fresh start accepts the pre-created empty observations
directory but rejects an existing session or observation history.

After container cleanup, both run controllers recursively restore owner write
permission throughout the `run/` directory they created, including on errors or
handled interruptions. Existing read/execute permissions are preserved, symlinks
are skipped, and permission errors are logged without changing the run's exit
status. Forced termination such as `SIGKILL` cannot perform this cleanup.

The Docker mounts are read-write so permission toggling works. This is a guardrail
against accidental edits, not an isolation boundary: the same user can deliberately
chmod files, and the short directory-write windows permit entry replacement or
deletion. A forced kill can leave a directory writable until the next client
command reseals it. Agents should put notes and transformed copies in their
writable workspace, leaving original observations alone. Existing runs keep their
copied client/layout; these changes apply to newly prepared runs.

### Timeouts

Game requests use a 10-second connection timeout and a 65-minute response read
timeout, including start, move, status, and description requests. The backend
operation timeout is one hour, leaving five minutes for a response or cleanup.
The labyrinth adapters use this for backend HTTP requests. New adapters should
use `ENVIRONMENT_OPERATION_TIMEOUT_SECONDS` from `environment_api.py` for
blocking build/setup and game operations; the server does not forcibly interrupt
manager code. These HTTP limits measure socket inactivity, not a total run budget.

The agent supervisor permits two hours without new `agent.log` output before
recovery. Build output in server logs does not reset that timer. Slow environment
setup belongs in `start_environment()`, after the HTTP server is ready.

The controller still allows 60 seconds for HTTP server readiness and uses short
health probes. Backend readiness probes and process shutdown waits also stay
short. Benchmark-specific task limits remain separate from these infrastructure
timeouts.

### Agent recovery and diagnostics

Each game's `run/agent.log` records agent-controller messages and Codex stdout
events. Codex stderr is streamed to both the terminal and `run/codex_stderr.log`.
Both logs are appended across continuations and recovery attempts. Stderr alone
does not reset the two-hour inactivity watchdog, which still watches `agent.log`.

Recovery first runs `client.py status`, saves its output in `agent.log`, and checks
the updated client state. A terminal session stops without launching Codex. At
step 0, recovery starts a new Codex thread with the normal initial prompt and the
status output instead of starting the game again. The new thread ID replaces the
old one; no previous thread ID is needed for step-0 recovery. After step 0, recovery
resumes the saved thread as before. The existing game, observations, and workspace
are retained. Failed startup without a saved client session is not recovered.

### Network isolation

Each experiment creates two shared networks: one internal Docker network for
all agents, servers, and the proxy, and one internet-facing network for servers
and the proxy. No per-game networks or host-published server ports are needed.
All servers listen on port 8879 inside their own containers; each client's
configuration points to its assigned server's unique container name.

Agents have no direct internet route. HTTPS access to OpenAI and ChatGPT goes
through the shared allowlisted Squid proxy. Agents can reach the other servers
on the shared internal network; this setup assumes nonadversarial agents.
The Codex account/API-key preflight checks run before these networks are created
and use normal Docker networking.

After the run finishes, generate score summaries with the analysis scripts.

## Analysis Scripts

The `analysis_scripts/` directory contains helper scripts for inspecting runs.

Extract raw actions from the per-game server logs (works with any environment):

```bash
python3 analysis_scripts/extract_raw_actions.py /path/to/run
```

This reads `/path/to/run/<alias>/server/server.log` and writes `/path/to/run/raw_actions.json`.
The output is a list of objects with `actions` (the unchanged applied-action
dictionaries in log order), `environment_id` (the real `game_id`), and `run_folder`
(the absolute per-game directory `/path/to/run/<requested_game_id>`, containing
`client/`, `observations/`, `run/`, and `server/`). Sessions are sorted by alias,
including sessions with no actions. Logs without a started session are skipped.
Re-running replaces the generated JSON.
Malformed action/session entries or actions without a session-start mapping are
reported as errors instead of silently producing incomplete histories.

Extract raw token counts from the per-game agent logs (without dollar estimates):

```bash
python3 analysis_scripts/extract_raw_cost_tokens.py /path/to/run
```

This writes `/path/to/run/raw_cost_tokens.json`, a list with `input_tokens`,
`cached_input_tokens`, `output_tokens`, `environment_id`, and `run_folder` for
each started session. Session order, real environment IDs, and absolute per-game
paths match the action extractor; `raw_actions.json` need not exist first.
The counts use the latest `turn.completed.usage` for each Codex thread and sum
across threads, matching the old analyser without double-counting resumed threads.
Incomplete turns cannot be counted from these logs. Threads with no reported
usage count as zero with a warning; missing logs are errors. Re-running replaces
the generated JSON.

Generate ARC3 scorecards by replaying the extracted actions:

```bash
python3 analysis_scripts/arc3/generate_scorecards_by_replay.py /path/to/run
```

This first runs `extract_raw_actions.py` with the same Python interpreter to
refresh `raw_actions.json`, then replays each game's actions using the host's
installed `arc-agi`/`arcengine` libraries. IDs are used unchanged, without resolving
historical game versions. Each game gets its own native SDK scorecard at
`/path/to/run/<environment_id>_scorecard.json` (for example, `ls20_scorecard.json`).
IDs must be unique within the run. Zero-action sessions are included; an
`agent.log` or per-game workspace is not required.

Replay uses normal mode with seed 0 and no rendering or recordings. The SDK may
download game files and use `ARC_API_KEY` from the host. `OPERATION_MODE=OFFLINE`
can use already downloaded games; online/competition modes are rejected so these
analysis replays do not become competition submissions. Only recorded actions
are replayed, including resets. Token/cost files are not read or modified.
Failed replays report the failing action and do not prevent other games from
running; the script exits nonzero if any game fails. Successful replays atomically
replace their previous scorecards; a failed replay leaves any old scorecard intact.

Run token extraction and ARC3 replay together with:

```bash
python3 analysis_scripts/arc3/analyse_runs.py /path/to/run
```

This runs `extract_raw_cost_tokens.py` followed by
`arc3/generate_scorecards_by_replay.py` with the same Python interpreter. Replay
also refreshes `raw_actions.json`. The wrapper stops on failure and returns a
nonzero exit code; Markdown printing remains a separate step.

After generating scorecards and raw token counts, print the ARC3 Markdown report:

```bash
python3 analysis_scripts/arc3/print_results_md_cost.py /path/to/run
```

This reads only `raw_cost_tokens.json` and `<environment_id>_scorecard.json` in
the run folder. It prints the old report's table and summary to stdout without
replaying games, reading logs or modifying files. Multiple run folders are
supported; a run-index column appears when games repeat across folders. Missing
scorecards/token records and duplicate game IDs within a run are reported as errors.

Estimated costs use the specified GPT-5.5 rates per million tokens: $5 uncached
input, $0.50 cached input, and $30 output. Cached tokens are subtracted from total
input before charging the uncached rate. These are estimates from the extracted
usage, not account billing. The report retains the old status heuristic:
`WIN` or at least 1,500 steps on the unfinished level means "normal termination";
otherwise it reports "interrupted". Scorecards alone do not establish why a run stopped.

Count attempts and accepted actions per level directly from ARC3 observations:

```bash
python3 analysis_scripts/arc3/count_level_attempts_dirs.py /path/to/run
```

This scans `/path/to/run/<alias>/observations/step_*_metadata.txt` and prints
`<alias> (<game_id>): 01/<attempts>/<actions>, 02/<attempts>/<actions>, ..., WIN`
in the old counter's format. `WIN` appears only for completed games. Each reached
level starts with one attempt; each subsequent `RESET` adds one attempt and counts
as an action. Step 0 is excluded, and a level-completing action belongs to the
level it completes. Empty pre-created observation folders show `no observations`.

The optional `<alias>/server/server.log` supplies real game IDs for labels only. Without
it, folder aliases are shown without guessing the game name. No extraction,
scorecards, agent logs, or ARC SDK are required, and no files are modified. Missing
steps and malformed metadata are errors rather than silently producing partial counts.

Summarize per-iteration progress for any environment:

```bash
python3 analysis_scripts/summarize_agent_logs_in_dir.py /path/to/run
```

This reads `<alias>/run/agent.log` (or `<alias>/agent.log`) and prints:

```text
<alias> (<real_game_name>)
iteration=1 step=10/42 protocol=normal_continuation_protocol
iteration=2 step=42/42 protocol=-
```

Only structured agent log records supply iterations, steps, and selected protocols.
`total_steps` is the highest logged global step counter for that game, including
recovery checkpoints; it is not an environment limit. A crash during a turn may
leave the last executed actions unreported. Protocols belong to the preceding
inspection, and `-` means no protocol was selected after it. Repeated iteration
IDs after recovery are retained in log order.

The optional `<alias>/server/server.log` maps aliases to real game names only; missing
mappings show `(unknown)`. No observations, session files, scorecards, token files,
or environment-specific packages are needed, and nothing is modified. Logs without
inspections are explicitly noted. Malformed logs are reported without preventing
other games from being summarized; the script exits nonzero if any log fails.

Check whether recovery was attempted in any game, for any environment:

```bash
python3 analysis_scripts/recovery_warning.py /path/to/run
```

This reads only `<alias>/run/agent_runner.log` (or `<alias>/agent_runner.log`)
and prints one warning per affected game, with the log path and number of recorded
recovery attempts, followed by a total. It counts explicit `recovery attempt N/M`
entries, not messages such as `not entering recovery` or the subsequent process
launch. Warnings do not imply recovery failed and do not change the exit code.
If no attempts are recorded, it reports how many logs were checked. Folders without
logs are skipped; missing input, no logs, or unreadable logs produce a nonzero exit
code. No files are modified, and no environment-specific packages are needed.

Analyse an agimaze_single run and print its results:

```bash
python3 analysis_scripts/agimaze_single/print_results_md_v1.py /path/to/run
```

The report unconditionally runs `agimaze_single/analyse_runs.py` before reading
its inputs. The analyser reruns `extract_raw_actions.py` and
`extract_raw_cost_tokens.py` on every invocation, refreshing both JSON files even
if they already exist. It needs `<alias>/server/server.log` and the per-game `agent.log`
files. Progress messages go to stderr, leaving stdout for the Markdown report.
You can also run `python3 analysis_scripts/agimaze_single/analyse_runs.py /path/to/run`
directly to refresh the analysis without printing a report.

After extraction, replay uses only `raw_actions.json` as its run-data input,
along with the bundled engine and maze files in
`src/server/environments/agimaze_single/labyrinth/`. Environment IDs must identify
exact maze files relative to `packs/`, not randomly selected directories. Use the
same maze definitions as the original run. No server subprocess or Docker is needed.

It writes `results.json` with the old fields: `name` (the raw `environment_id`),
`labyrinth_index` (zero-based raw-record order), `steps`, `max_steps`, and `solved`.
Here `max_steps` is the original x1 budget, not the runtime's x5 cap. Zero-action
games remain in the results as unsolved; only started sessions are included.
If extraction or any replay fails, the command exits nonzero and leaves any
previous `results.json` unchanged instead of writing a partial report. The report
also exits nonzero if analysis fails; it never falls back to stale results.

After refreshing analysis, Markdown rendering reads only `results.json` and
`raw_cost_tokens.json`, preserving
the old x5/x2/x1 solved counts, Score1 formula, table, and summary, and appending
`total cost: $...`, then a blank line and a numbers-only summary:
`score x5_unsolved/x2_unsolved/x1_unsolved total_actions total_cost`.
Here `score` is average Score1 (three decimals); cost has two decimals and no
currency symbol or thousands separators. It uses the ARC3 report's GPT-5.5 rates: $5 per million uncached
input tokens, $0.50 per million cached input tokens, and $30 per million output
tokens. Cached input is included in the input count and is not charged twice.
Token records must match the mazes in `results.json`; missing or duplicate records
are errors. Category ordering is built in rather than read from `order.json`;
new stages and maze filenames sort naturally. Rendering does not read observations,
agent/server logs, raw actions, or maze definitions directly.
Both scripts use only the Python standard library and the bundled replay code.

For curriculum runs in `agimaze_set_v1`, score only the last maze of each set:

```bash
python3 analysis_scripts/agimaze_sets/print_ts_results_md_v1.py /path/to/run
```

This unconditionally runs `agimaze_sets/analyse_ts_runs.py`, which refreshes both
`raw_actions.json` and `raw_cost_tokens.json` with the existing extractors. You
can also run the analyser directly without printing a report:

```bash
python3 analysis_scripts/agimaze_sets/analyse_ts_runs.py /path/to/run
```

Each raw `environment_id` names a definition in
`src/server/environments/agimaze_set_v1/sets/<environment_id>.json`. The analyser
requires exactly one `start` action per maze, in addition to requiring that the
action stream begins with `start`. Too few or too many starts are errors, not
silently skipped targets. This checks that every maze was started, not that every
maze was solved; the environment also advances after exhausting a maze's budget.
Set and maze definitions must match those used for the run.

It writes `last_maze_actions.json` with just the actions after the final `start`
of each set. Each record retains `run_folder`, uses the target maze path as
`environment_id`, and adds the original `set_id` and the target's zero-based
`labyrinth_index` within the set. Replaying these actions with the single-maze
engine produces `last_maze_results.json`, with `name`, `steps`, `max_steps`,
`solved`, `set_id`, and the same within-set `labyrinth_index`. No curriculum
actions or `start` commands count towards target-maze steps. A started target
with no moves is included as unsolved. Validation and replay of all targets
finish before either derived file is replaced; failures stop the report.

Rendering reads only `last_maze_results.json` and `raw_cost_tokens.json`. It uses
the same target-maze ordering, x5/x2/x1 metrics, and Score1 formula as the single
report. Costs are matched by **set ID** and cover the **whole curriculum run**,
not just the final maze; the total is labelled accordingly. After a blank line,
the report ends with the same numbers-only summary as the single-maze report,
using target-maze metrics and whole-set cost. Different sets with
the same target remain separate rows, labelled with their set IDs. Progress goes
to stderr and Markdown to stdout. There is no dependency on observations or Docker.

Replay one maze from an action JSON file, either as text or in the terminal player:

```bash
python3 analysis_scripts/agimaze_single/replay_single.py /path/to/raw_actions.json TRAINING/S0-keys/STAGE-01/0000.json
python3 analysis_scripts/agimaze_single/replay_single_terminal.py /path/to/raw_actions.json TRAINING/S0-keys/STAGE-01/0000.json
```

Both accept a JSON file containing a list of dictionaries and an exact maze name.
They infer the name field from a matching top-level string value (`environment_id`,
`name`, or another key), require exactly one matching record and one matching key,
and replay that record's `actions` list. Missing, duplicate, or ambiguous matches
are errors. Empty action lists show the initial state only. No results file, run
folder layout, observations, or original server session is needed; the input JSON
is not modified. Maze names must identify exact files within the bundled `packs/`.

Both commands launch and clean up a separate local labyrinth backend with debug
maps. The text version prints every step; the terminal version collects the same
frames before opening its interactive player. Use left/right to step, up/down to
jump to the start/end, space to play/pause, and `q` to quit. Optional flags are
`--host` (default `127.0.0.1`), `--port` (default `8000`), `--lang` (default `en`),
and `--timeout` (startup seconds, default `10`). The terminal player also accepts
`--delay` (seconds per frame, default `0.15`). Use a different port if it is already
in use. Actions after the maze has ended are reported as an error, not discarded.

- `get_comptetiotion_scorecard_from_id.sh` downloads a competition scorecard by ID when you run in competition mode.

## Running in Competition Mode

To run ARC3 in competition mode, set the environment parameters in `run_config.yaml`:

~~~yaml
environment: arc3
environment_parameters:
  competition: true
  tag: api-test
environment_variables: [ARC_API_KEY]
~~~

Then run `python3 run_controller.py` normally. The old `--competition` flag and top-level `tag`/`competition` settings are no longer supported.

There are two important caveats:

1. A competition scorecard can be closed after about 15 minutes of inactivity. In practice the limit may be somewhat longer, but the agent can spend more than 15 minutes in simplification or refactoring prompts, especially when Codex is slow. This makes competition mode better suited to large parallel runs.

2. A competition scorecard closes after 24 hours. Some individual games can take more than 24 hours, and you may not have enough ChatGPT Pro subscriptions to run all games in parallel.

## API-Key Controller

There is also `run_controller_with_api_key.py`, which runs Codex with `OPENAI_API_KEY` instead of authenticated Codex account folders.

It uses the same `environment`, `environment_parameters`, and `environment_variables` configuration. `OPENAI_API_KEY` is a separate agent credential; it does not belong in `environment_variables` unless the environment itself also needs it.

This mode is expected to be much more expensive. In earlier testing, prompt-cache hit rates were much lower with API-key runs than with ChatGPT Pro-backed Codex accounts. Because of this, runs that fit within the weekly limit of a single ChatGPT Pro account may cost around 5000 USD if run through the API. The script prints a warning and requires explicit confirmation before it starts.
