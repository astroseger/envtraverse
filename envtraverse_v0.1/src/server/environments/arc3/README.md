# ARC3

Install this environment's dependencies in the server's Python environment:

~~~bash
python3 -m pip install -r src/server/environments/arc3/requirements.txt
~~~

Each manager owns one game, one Arcade instance and, in competition mode,
one scorecard for that game. Configure credentials and game data as required by
your ARC3 setup. Other environments do not import these dependencies.

With either run controller, select `environment: arc3` and list
`environment_variables: [ARC_API_KEY]` in `run_config.yaml`. Export the key on the
host; do not put its value in YAML or `environment_parameters`. The controller
forwards it only to the server container. Non-competition mode uses the SDK's
normal mode, which can still need a key and network access to download games.

The constructor only validates configuration. `start_environment()` initializes
the backend, opens the competition scorecard, and returns the description and
initial observation. The manager owns the SDK environment wrapper and counters;
the server calls its `move(action)` directly.

Only one start attempt is allowed. `stop()` releases the wrapper and finalizes
the game's scorecard once, including after a startup failure. It is safe to call
before startup or repeatedly. A new game run needs a new manager.

Parameters:

- `competition`: boolean, use competition mode (default `false`). Use YAML
  `true` or `false`, not quoted strings such as `"false"`.
- `source_url`: string, source URL attached to competition scorecards (default
  `https://github.com/astroseger/arc-3-agents-baseline1`). This is sent to the
  scorecard service; the server does not add it to client responses.
- `tags`: scorecard tags as a string or list of strings (`tag` is also accepted).

Unknown parameters and incorrect types are rejected before backend initialization.
Specify only one of `tags` or `tag`. Omitted, null, or empty tags use the default
`["api-test"]`; a blank `source_url` uses the default URL. Strings are trimmed,
but numbers, booleans, and other values are not converted to strings.

The manager supplies `environment_description.md` from this directory. Frame
rendering stays in `artifacts_preprocessing.py`; it returns text and PNG bytes.
The server handles base64 encoding and response caching.

`to_agent_workspace/ascii_to_png.py` is an optional workspace helper. Both run
controllers copy it to the agent workspace when ARC3 is selected. For direct
server/client launches, copy it manually if needed; the HTTP server does not
distribute helper programs.

Scorecard outputs continue to use `ENV_SCORECARD_PATH` and
`ENV_FINAL_SCORECARD_PATH`. A missing action frame is treated as an explicit
fatal backend error that ends this game's server.

Descriptions and server-generated errors use neutral wording. Observation
metadata uses the original serialization without additional validation or
redaction. The environment controls the values it exposes, including action data.

Server-side logs retain backend names, game and scorecard identifiers, paths,
and full exception tracebacks. These diagnostics are not included in client
responses. Logs, scorecard files, and backend recordings remain private server
data and must not be copied into the agent workspace.
