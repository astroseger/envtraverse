# Single-game environment server

Each server process serves exactly one game run. It owns one environment manager,
one session token, and one cached response. A new run needs a new server process.

Choose the environment package with `--environment`, the private game ID with
`--game-id`, and the public client alias with `--game-alias`. The server accepts
one start attempt for that alias. The real game ID stays in private server
configuration and logs.

Both run controllers launch one server container per active game with these
arguments. Each has its own `run/<alias>/server/` mounted at `/data/`, while all
pairs share the experiment's internal and internet networks. The controller
waits for that server's health check before launching its agent and stops the
server when the agent exits.

## Direct launch

Install the common dependencies:

~~~bash
python3 -m pip install -r src/server/requirements.txt
~~~

ARC3 also needs `src/server/environments/arc3/requirements.txt`. The example and
labyrinth adapters use Python's standard library. The client requires `requests`.

From the repository root:

~~~bash
ENV_SERVER_LOG_PATH=/tmp/example-server.log \
python3 src/server/server.py --environment example \
  --game-id counter --game-alias game1 --parameters '{"target":3}'
~~~

The log path must not already exist. Other environments:

~~~bash
ENV_SERVER_LOG_PATH=/tmp/labyrinth-server.log \
python3 src/server/server.py --environment agimaze_single \
  --game-id TRAINING/S0-keys/STAGE-01/0000.json --game-alias game1 \
  --parameters '{"seed":42}'

ENV_SERVER_LOG_PATH=/tmp/labyrinth-set-server.log \
python3 src/server/server.py --environment agimaze_set_v1 \
  --game-id set01 --game-alias game1

ENV_SERVER_LOG_PATH=/tmp/arc3-server.log \
python3 src/server/server.py --environment arc3 \
  --game-id YOUR_ARC3_GAME_ID --game-alias game1 --parameters '{"competition":false}'
~~~

Use `--host` and `--port` to change the default address, `127.0.0.1:8879`.
Their defaults can also be set with `ENV_SERVER_HOST` and `ENV_SERVER_PORT`.
Each server needs its own log and scorecard paths. Servers in separate containers
can use the same internal ports; direct concurrent host processes need distinct
HTTP ports and, for labyrinth, distinct backend `port` parameters.

To test the client, prepare a fresh copy and empty observations directory. Do not
run the client in the source checkout: it changes its directory permissions.

~~~bash
client_test_dir=$(mktemp -d)
cp -R src/agents/agent_v1/agent/client "$client_test_dir/client"
mkdir "$client_test_dir/observations"
python3 -c 'from pathlib import Path; import sys; from src.agents.agent_v1.agent.client.client_storage import initialize_client_storage; root = Path(sys.argv[1]); initialize_client_storage(root / "client", root / "observations", server_url="http://127.0.0.1:8879")' "$client_test_dir"
python3 "$client_test_dir/client/client.py" start game1
python3 "$client_test_dir/client/client.py" move '{"id":"INC"}'
python3 "$client_test_dir/client/client.py" status
~~~

The client saves the authoritative description as `environment_description.md`
and observation artifacts under its configured observations directory. Its
session state and read-only `client_config.json` live beside the copied client.
Storage is read-only between client writes. Client requests use the configured
server URL and ignore proxy environment settings.

## Environment interface

Copy `environments/example/` into a new package and export an ordinary
`EnvironmentManager` class from `environment_manager.py`. Import
`Observation`, `StartResult`, and exceptions from `environment_api`.
Package-local imports should be relative.

The manager owns the game state and all backend resources:

- `__init__(parameters)`: store and validate configuration. Do not start
  subprocesses, SDK sessions, or scorecards here.
- `start_environment(game_id) -> StartResult`: attempt to start the instance's
  only game. Return its description and initial observation. Clean up partial
  resources if startup fails.
- `move(action) -> Observation`: apply one action to the running game.
- `stop()`: release all resources. It must work before startup, after failed
  startup, and after completion, and must be safe to call repeatedly.

A manager permits one start attempt. Starting after `stop()` is also rejected.
Create a new instance for another run. No base class is required; the shared
`EnvironmentManager` is a typing protocol.

~~~python
return StartResult(
    environment_description="Game rules and available actions...",
    observation=Observation(files={"state.txt": "Initial state"}),
)
~~~

There is no returned game object or ownership transfer. The server calls the
manager's `move()` and `stop()` directly. Private helper objects are optional.

The example manager holds its counter directly. ARC3 initializes one Arcade
instance and, in competition mode, one scorecard for the game during startup.
Its `stop()` finalizes that scorecard. The single-labyrinth manager owns one
subprocess on a fixed port. A labyrinth-set manager owns at most one child
manager, stops it between mazes, and creates the next child on the same port.
The whole set is one logical game run.

An observation is `Observation(files={"state.txt": text, "frame.png": png_bytes},
terminal=False)`. Contents must be `str` or `bytes`. Names must be plain
filenames, without path separators or NUL characters; an empty file mapping is
allowed. The server validates and UTF-8/base64 encodes artifacts. Environment
implementations own rendering and rules; clients own local artifact persistence.

The server retains the last successful response and description through the
terminal state. It stops the manager when the process shuts down. Invalid
actions do not advance the server's step counter.

## Execution and errors

Construction, startup, actions, and cleanup all execute on the server's main
thread. HTTP requests run synchronously with one worker. There are no owner
threads, request queues, locks, or session registries. Do not wrap this app in
a threaded or multi-process HTTP deployment.

A slow action also delays this server's health and status requests. Other game
servers have independent execution. All client game requests use a 10-second
connection timeout and a 65-minute response read timeout. The agent supervisor
allows two hours without new `agent.log` output before recovery.

Adapters should use `ENVIRONMENT_OPERATION_TIMEOUT_SECONDS` (one hour), exported
by `environment_api.py`, for blocking setup/build and game operations. The
labyrinth adapters use it for backend HTTP requests. The client allows an extra
five minutes for returning results or cleaning up. HTTP read timeouts measure
socket inactivity; they are not a total execution deadline. The server does not
forcibly interrupt manager code or override third-party SDK timeouts.

Long initialization belongs in `start_environment()`. The controller's
60-second readiness deadline applies only to the HTTP server before game
initialization. Individual readiness probes and shutdown waits stay short.

A client timeout does not cancel an action already running on the server.
Recover the last step before deciding whether to send another action. Initial
startup cannot be recovered if it times out before the client saves its token.
Benchmark-specific task execution limits are separate from these waits.

- `InvalidActionError`: rejected action, HTTP 400; raise before changing state.
- `EnvironmentStartError`: startup failure, HTTP 400.
- `EnvironmentError`: operational failure, HTTP 503.
- `FatalEnvironmentError`: unusable backend, HTTP 503 followed by shutdown.
- Unexpected exceptions or invalid results: HTTP 500 with a generic message.

Any failed initial start stops the manager and ends the server; startup cannot
be retried in the same process. A fatal response is sent before normal cleanup
and process exit. SIGINT and SIGTERM also run cleanup.

Explicit environment exception messages reach clients. Keep private backend
identifiers and diagnostics in chained exceptions and server logs. A failed move
does not advance the server's counter, but the backend might already have
changed state; replaying an action is not automatically safe.

## HTTP

- `GET /health`: server availability; the game starts only on `/game/start`.
- `POST /game/start`: accepts `{"game_id":"public-alias"}` and returns
  `session`, `environment_description`, and initial `step`.
- `POST /game/action`: accepts `{"action":{...}}` and returns `session`,
  `action`, and `step`.
- `GET /game/last-step`: the last successful start/action response.
- `GET /game/environment-description`: cached
  `{"environment_description":"..."}`.

All game routes except start require the `X-Session-Token` header.
Artifacts use `{"name":"state.txt","content_base64":"..."}`.

## Packages and tests

An optional environment-local `requirements.txt` supplies dependencies for the
server image. `to_agent_workspace/` supplies public helper files; keep private
configuration, credentials, and scorecards out of it. The environment description
comes from the start result.

Use `--parameters` for non-secret JSON settings. Direct launches inherit their
shell's environment for credentials. Build an environment image with
`./src/build_dockers.sh example agent_v1` or another package name.

Run the server, environment, and client integration tests:

~~~bash
python3 -m unittest discover -s src/server/tests -v
~~~
