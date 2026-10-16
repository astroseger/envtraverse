# Run analysis

These scripts extract recorded actions and token usage, replay game runs, and
produce reports. Run the examples from this directory. See
[Running experiments](../docs/running/README.md) for setup and execution examples.

Most commands take a run directory such as `/path/to/experiment/run`.
The Terminal-Bench report takes the **parent experiment directory** containing
both `run/` and `run_tb/`.

Extraction and replay refresh derived JSON files in the run directory. Keep the
original logs and the environment definitions used for the experiment when
archiving results.

## Shared extraction

Extract accepted environment actions:

~~~bash
python3 extract_raw_actions.py /path/to/run
~~~

This reads `<alias>/server/server.log` and writes `raw_actions.json`. Each
record contains `environment_id` (the real task ID), `run_folder` (the absolute
per-task directory), and the unchanged `actions` in applied order. Started
sessions with no actions are included; logs without a started session are skipped.
Malformed action/session records are errors.

Extract token usage:

~~~bash
python3 extract_raw_cost_tokens.py /path/to/run
~~~

This uses the server logs to identify sessions and reads their agent logs. It
writes `raw_cost_tokens.json` with `environment_id`, `run_folder`,
`input_tokens`, `cached_input_tokens`, and `output_tokens`.
It does not require `raw_actions.json`.

Counts use the latest reported `turn.completed.usage` for each Codex thread,
summed across threads. Resumed threads are not counted repeatedly. Incomplete
turns have no usage record; threads with no reported usage count as zero with a
warning. Missing agent logs are errors.

Both extractors replace their generated JSON on a successful rerun. They use
the Python standard library and work across environments.

## ARC-AGI-3

Install the replay dependencies in the host Python environment:

~~~bash
python3 -m pip install -r ../src/server/environments/arc3/requirements.txt
~~~

Refresh token counts, extract actions, replay games, and print the report:

~~~bash
python3 arc3/analyse_runs.py /path/to/run
python3 arc3/print_results_md_cost_v2.py /path/to/run
~~~

The analysis command writes `raw_cost_tokens.json`, `raw_actions.json`, and
one `<environment_id>_scorecard.json` per game. The report reads only scorecards
and token counts and prints completion, RHAE, action counts, and normalized cost
tokens. It accepts multiple run directories:

~~~bash
python3 arc3/print_results_md_cost_v2.py /path/to/first/run /path/to/second/run
~~~

Replay uses the host's ARC SDK in normal mode with seed 0. It uses recorded IDs
unchanged, so retain the corresponding game versions. It may need `ARC_API_KEY`
and download access. Set `OPERATION_MODE=OFFLINE` to use already downloaded
games. Online/competition replay modes are rejected.

Only recorded actions, including RESET, are replayed. A failed replay reports the
action index and leaves any previous scorecard for that game intact; check the
analysis command's exit status before generating a report. The analysis wrapper
stops if extraction fails, and replay exits nonzero if any game fails.

To refresh only actions and replayed scorecards:

~~~bash
python3 arc3/generate_scorecards_by_replay.py /path/to/run
~~~

To count attempts and actions directly from saved observations:

~~~bash
python3 arc3/count_level_attempts_dirs.py /path/to/run
~~~

The counter reads `<alias>/observations/step_*_metadata.txt` and prints entries
such as `01/2/4`: level 1, two attempts, four actions. Step zero is excluded;
RESET counts as an action, and a level-completing action belongs to that level.
It needs no SDK or generated scorecards and modifies no files.

## AGI Maze: single labyrinths

Refresh analysis and print a report:

~~~bash
python3 agimaze_single/print_results_md_v2.py /path/to/run
~~~

The report first runs the analyser, which refreshes action/token extraction and
replays every recorded maze using the bundled engine and packs. It needs no Docker
or separate server process. Task IDs must identify exact maze files relative to
[packs/](../src/server/environments/agimaze_single/labyrinth/packs/); use the
definitions from the original experiment.

Analysis writes `results.json` with `name`, `labyrinth_index`, `steps`,
`max_steps`, and `solved`. Here `max_steps` is the original reference budget;
the runtime permits five times that budget. Zero-action sessions are included as
unsolved. Extraction or replay failure stops the command and preserves any previous
results file.

The v2 report excludes IDs starting with `TRAINING`. It displays x5/x2/x1 solved
counts, the penalized step ratio, and normalized cost tokens. The ratio is
`steps / max_steps` for solved mazes and `5` for unsolved mazes; lower is better.

To refresh results without printing a report:

~~~bash
python3 agimaze_single/analyse_runs.py /path/to/run
~~~

## AGI Maze sets

For `agimaze_set_v1` runs:

~~~bash
python3 agimaze_sets/print_ts_results_md_v2.py /path/to/run
~~~

This refreshes extraction and evaluates only the **final maze of each set**.
Costs cover the **whole set**, including preceding tutorials. The report uses the
same ratios and solved counts as the single-maze v2 report and excludes sets whose
final maze is a `TRAINING` maze.

Each recorded set ID must match a definition in
[sets/](../src/server/environments/agimaze_set_v1/sets/). The analyser requires
one `start` action for every maze and an action stream beginning with `start`.
A missing target start is an error. Reuse the original set and maze definitions.

Generated files are:

- `last_maze_actions.json`: actions after the final `start`, with the target
  path as `environment_id`, plus `set_id`, `labyrinth_index`, and `run_folder`.
- `last_maze_results.json`: replayed target results with `name`, `steps`,
  `max_steps`, `solved`, `set_id`, and `labyrinth_index`.

Tutorial actions and `start` commands do not count toward target steps.
Different sets targeting the same maze remain separate records.
To generate these files without a report:

~~~bash
python3 agimaze_sets/analyse_ts_runs.py /path/to/run
~~~

## Terminal-Bench

Wait for host-runtime grading to finish, then pass the experiment directory:

~~~bash
python3 terminal_bench/print_results_md_cost.py /path/to/experiment
~~~

The script refreshes `run/raw_cost_tokens.json` and reads
`run_tb/results/<alias>/result.json`, together with the matching
`run/<alias>/run/agent.log` and `agent_runner.log` files. Preserve both output
directories together when archiving a Terminal-Bench experiment.

The report shows per-task reward and recovery counts, mean reward, normalized
cost tokens, cybersecurity-related error counts, and outcomes grouped by
termination reason. Null or nonnumeric rewards count as zero. A non-normal final
runner-log message is classified as failed recovery; this is a log-based
diagnostic, separate from the verifier reward.

It does not replay tasks or rerun verification. See the
[Terminal-Bench guide](../README_tb.md) for runtime shutdown and grading.

## Report versions and cost conventions

The scripts retain two report formats:

| Environment | Report with normalized cost tokens | Earlier report |
| --- | --- | --- |
| ARC-AGI-3 | [arc3/print_results_md_cost_v2.py](arc3/print_results_md_cost_v2.py) | [arc3/print_results_md_cost.py](arc3/print_results_md_cost.py) |
| AGI Maze single | [agimaze_single/print_results_md_v2.py](agimaze_single/print_results_md_v2.py) | [agimaze_single/print_results_md_v1.py](agimaze_single/print_results_md_v1.py) |
| AGI Maze sets | [agimaze_sets/print_ts_results_md_v2.py](agimaze_sets/print_ts_results_md_v2.py) | [agimaze_sets/print_ts_results_md_v1.py](agimaze_sets/print_ts_results_md_v1.py) |

Terminal-Bench's [report](terminal_bench/print_results_md_cost.py) also uses
normalized cost tokens. The formula is:

~~~text
cost_tokens = cached_input_tokens / 60
            + (input_tokens - cached_input_tokens) / 6
            + output_tokens
cost_tokens_M = cost_tokens / 1_000_000
~~~

Cached input is already included in `input_tokens`. These fixed weights are a
comparison convention, not account billing.

The earlier reports instead estimate dollar cost using fixed coefficients of
$5 per million uncached input tokens, $0.50 per million cached input tokens, and
$30 per million output tokens. These are the scripts' stored assumptions, not a
statement of current model pricing.

The earlier maze reports include `TRAINING` entries and use Score1: zero for
unsolved mazes, otherwise `1 / (1 + max(0, steps / max_steps - 1) ** 2)`.
They also print total actions and a final numeric summary. The earlier ARC report
uses a completion heuristic to label runs as normal or interrupted; scorecards
alone do not establish the reason a run stopped.

Neither maze report version computes the paper's RHAE70 metric. The v1 Score1
and v2 penalized step ratio should be identified by their own names when reporting
results.

## Progress and recovery diagnostics

These commands read logs without modifying run files:

~~~bash
python3 summarize_agent_logs_in_dir.py /path/to/run
python3 recovery_warning.py /path/to/run
~~~

The progress summary reads agent logs and prints iterations, observed global
steps, and continuation protocols. Its total step count is the highest logged
counter, not an environment budget. Repeated iteration IDs after recovery remain
in log order; a crash can leave the latest actions unreported.

The recovery check reads `agent_runner.log` and counts explicit recovery-attempt
entries. A warning means recovery was attempted, not that it failed.
Both commands support the bundled `<alias>/run/` log layout and older logs
directly under `<alias>/`.

## Replay an individual maze

Replay one record from an action JSON file:

~~~bash
python3 agimaze_single/replay_single.py \
  /path/to/raw_actions.json TUTORIAL/STAGE-01/0000.json
python3 agimaze_single/replay_single_terminal.py \
  /path/to/raw_actions.json TUTORIAL/STAGE-01/0000.json
~~~

Both require exactly one record matching the maze name and containing an
`actions` list. They also accept `last_maze_actions.json` when the target is
unique. The input file is unchanged.

Both launch and clean up a local labyrinth backend with debug maps. The text
version prints each step; the terminal player lets you use left/right to step,
up/down to jump to the start/end, space to play/pause, and `q` to quit.

Options include `--host` (default `127.0.0.1`), `--port` (default `8000`),
`--lang` (default `en`), and `--timeout` (startup seconds, default `10`).
The terminal player also accepts `--delay` (seconds per frame, default `0.15`).
