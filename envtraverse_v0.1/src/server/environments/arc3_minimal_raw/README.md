# ARC3 with raw JSON observations

ARC-AGI-3 with a minimal task description and raw SDK observations.
For experiment commands, see
[Running experiments](../../../../docs/running/README.md#arc-agi-3-observation-variants).

The runner installs this adapter's dependencies in the server image. For a
direct server launch, install them from `envtraverse_v0.1/`:

~~~bash
python3 -m pip install -r src/server/environments/arc3_minimal_raw/requirements.txt
~~~

Select `environment: arc3_minimal_raw` and set
`environment_variables: [ARC_API_KEY]` in the run configuration. Export the key
on the host; the controller forwards it only to the server container. Normal
mode can still need a key and network access to download games.

Parameters:

- `competition`: boolean, use competition mode (default `false`).
- `source_url`: string attached to competition scorecards (default
  `https://github.com/astroseger/arc-3-agents-baseline1`).
- `tags`: scorecard tags as a string or list of strings; `tag` is also accepted.
  The default is `["api-test"]`.

Parameter validation follows the [ARC3 adapter](../arc3/README.md). Unknown
parameters and incorrect types are rejected before backend initialization;
specify only one of `tags` or `tag`.

The manager supplies [environment_description.md](environment_description.md).
Initialization and every action return one `observation.json` file. It contains
the full raw SDK response, including `game_id`, `guid`, `state`, level counts,
`full_reset`, numeric action IDs, and the complete `action_input`, including
`reasoning`. Every frame is included as nested numeric lists under `frame`, in
its original order. These arrays are added explicitly because
`FrameDataRaw.model_dump()` excludes its private frame data. SDK fields and
values are preserved without additional validation or redaction.

Use `agent_v1` or `agent_v1_01_wstop` with this format. The bundled
Controlled_RESET agent reads `metadata.txt`, which this adapter does not emit.

Each manager owns one game and initializes the backend in
`start_environment()`. `stop()` releases the wrapper and finalizes the game's
competition scorecard once, including after a startup failure. It is safe to
call before startup or repeatedly.

Scorecard output paths and private server diagnostics follow the
[ARC3 adapter](../arc3/README.md). Observation files are produced by
[artifacts_preprocessing.py](artifacts_preprocessing.py); the shared server
handles observation encoding and response caching.
