# Environment server: HTTP protocol

The server exposes one environment session through HTTP. It translates requests
into calls to an environment adapter and returns descriptions, observation files,
and terminal status. To implement an environment, see
[Environments](environments/README.md). For the overall interaction model, see
the [framework README](../../README.md).

Each server serves one task, with one start attempt and one session token. The
runners create a server for each active task and pass its URL and a public task
alias to the agent's preparation hook. The real task ID stays in the server's
configuration. A new run requires a new server.

## Endpoints

POST bodies are JSON objects sent with `Content-Type: application/json`.
All `/game/` routes except `/game/start` require the `X-Session-Token` header.

| Method and path | Request | Successful response |
| --- | --- | --- |
| `GET /health` | No token or body | `status: "ok"` and `server_started` timestamp |
| `POST /game/start` | `{"game_id":"public-alias"}` | `session`, `environment_description`, and initial `step` |
| `POST /game/action` | `{"action":{...}}` | `session`, submitted `action`, and resulting `step` |
| `GET /game/last-step` | Session token | The last successful start or action response |
| `GET /game/environment-description` | Session token | `{"environment_description":"..."}` |

The health endpoint reports server availability; it does not start the task.

## Start a session

Send the public alias supplied by the runner, even though the request field is
named `game_id`:

```json
{"game_id":"game1"}
```

An example response for the counter environment, with the description shortened:

```json
{
  "session": {
    "token": "0123456789abcdef0123456789abcdef",
    "terminal": false
  },
  "environment_description": "Reach 3 by incrementing a counter. Send {\"id\":\"INC\"} to increment.",
  "step": {
    "step_index": 0,
    "artifacts": [
      {
        "name": "state.txt",
        "content_base64": "dmFsdWU6IDAKdGFyZ2V0OiAzCg=="
      }
    ]
  }
}
```

The description explains the task and its action interface. Save the token and
include it in subsequent game requests. The initial observation has
`step_index: 0`; initialization may already return a terminal session.

Additional public files, if the environment supplies them, are placed in the
agent workspace by its preparation hook before launch. They are separate from
the HTTP start response.

## Submit an action

For the counter environment, send the following to `/game/action` with the
session header:

```json
{"action":{"id":"INC"}}
```

The response echoes the action and returns the next observation:

```json
{
  "session": {
    "token": "0123456789abcdef0123456789abcdef",
    "terminal": false
  },
  "action": {"id":"INC"},
  "step": {
    "step_index": 1,
    "artifacts": [
      {
        "name": "state.txt",
        "content_base64": "dmFsdWU6IDEKdGFyZ2V0OiAzCg=="
      }
    ]
  }
}
```

The action must be a JSON object. Its fields and meaning are defined by the
environment, not by the HTTP server. Each successful action advances the step
index by one.

Observation artifacts are named files with base64-encoded contents. Decode them
to bytes; textual files are encoded as UTF-8 before base64 encoding. Names contain
no directory components. A response may contain multiple files or an empty list.
The example above decodes to `value: 1\ntarget: 3\n` in `state.txt`.

When `session.terminal` becomes `true`, further actions are rejected. The last
response and description remain available while the server is running.
Termination does not itself indicate success; scoring and success criteria
belong to the environment.

## Errors and interrupted requests

Application errors return an appropriate HTTP status and a JSON message:

```json
{"error":"Invalid action: action must be a JSON object"}
```

| Status | Meaning |
| --- | --- |
| `400` | Invalid request, missing or invalid session token, rejected action, or failed environment initialization |
| `503` | Environment operation failed or the server is shutting down; a fatal backend failure ends the server |
| `500` | Unexpected error; private details remain in the server log |

Routing errors such as an unknown URL can use the HTTP framework's default error
response. A failed environment initialization ends the server; start a fresh
server for another attempt.

Requests are processed sequentially. A slow action also delays that server's
health and status requests. A client timeout does not cancel work already running
on the server.

After an interrupted action request, use `/game/last-step` and its step index to
check whether a response was recorded before deciding what to do next. It returns
the cached response without applying an action. There is no request-ID-based
deduplication or automatic replay: an operational failure may have changed the
backend even when no new response was recorded. Initialization cannot be
recovered through this endpoint if the client never received the session token.

## Try the protocol locally

Run these commands from `envtraverse_v0.1/`. The counter environment needs no LLM,
Docker, or environment credentials:

```bash
python3 -m pip install -r src/server/requirements.txt
ENV_SERVER_LOG_PATH=/tmp/envtraverse-counter.log \
python3 src/server/server.py --environment example \
  --game-id counter --game-alias game1 --parameters '{"target":3}'
```

Choose an unused log filename for each run. The server listens on
`127.0.0.1:8879` by default; use `--host` and `--port` to change it.

In another terminal, start the session:

```bash
curl --noproxy '*' http://127.0.0.1:8879/game/start \
  -H 'Content-Type: application/json' \
  -d '{"game_id":"game1"}'
```

Copy `session.token` from the response, then submit an action:

```bash
session_token='paste-the-returned-token'
curl --noproxy '*' http://127.0.0.1:8879/game/action \
  -H 'Content-Type: application/json' \
  -H "X-Session-Token: $session_token" \
  -d '{"action":{"id":"INC"}}'
```

Two further `INC` actions reach the target and return `terminal: true`. Stop the
server with Ctrl+C. See the [agent guide](../agents/README.md) to connect your own
agent or use the bundled client.
