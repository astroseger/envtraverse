# Omega agent v1

This first version sends one task prompt to the full Omega runtime from
`astroseger/Omega`, branch `chatgpt-subscription`. It runs `agent.py` directly,
with no recovery supervisor, continuation prompts, or stuck protocol. Omega's
source, tools, stock system prompt, and autonomous loop are unchanged.

`maxNewInputLoops=10000000` gives the initial task up to 10 million active model cycles.
`maxWakeLoops=0` disables scheduled model calls afterward. Reaching the cycle
limit makes Omega idle; it does not exit the container or establish task success.
Omega itself does not exit when the environment becomes terminal. The Omega
controller stops it after the configured server terminal grace period. With the
manual launcher, stop the container when the test is finished. Model requests
use the selected Omega ChatGPT account's plan allowance.

## Docker and Python

The image starts from Ubuntu 24.04 and installs the same scientific apt
packages as `agent_v1`. SWI-Prolog 10.0.2 is compiled for Ubuntu's Python 3.12,
then PeTTa, FAISS, and Omega are installed directly. It does not require the
`omega_accounts` image or an existing local `agent_v1` image.

Everything shares **one system Python 3.12 installation**. There are no virtual
environments or activation steps:

| Command | Purpose |
| --- | --- |
| `python` / `python3` | Scientific code, unchanged client, and Omega dependencies |
| `swipl` | SWI-Prolog, including Janus using the same Python packages |
| `metta FILE.metta` | PeTTa from any working directory; its Python calls use those same packages |

`$AGENT_PYTHON` and `$OMEGA_PYTHON` both point to `/usr/bin/python3`. NumPy is
constrained to `1.26.4` to preserve compatibility with Ubuntu's compiled science
packages. Omega can update other Python dependencies, including Pydantic, SymPy,
and PyTables, so their versions need not match the original `agent_v1` image exactly.
For apt-managed dependencies, the build resolves the needed updates and installs
their wheels globally without asking pip to uninstall the original apt files.
The resolved versions are recorded in `/opt/agent-requirements-resolved.txt`.
PyTables is upgraded to 3.11 or later so imports work inside Janus as well as
ordinary Python. Every build checks dependencies and exercises all 48 scientific
imports plus Omega's main dependencies through ordinary Python and embedded Janus.

From the envtraverse directory:

```bash
./src/agents/omega_agent_v1/build_docker.sh
```

This builds `omega-agent-v1:UID-GID` with the host user's identity. An explicit
build resolves the latest `chatgpt-subscription` commit, then fetches that exact
commit inside Docker. Use `--if-missing` to reuse an existing image. Rebuild
without that flag after pushing Omega changes or replacing an older agent image.
The helper uses a shared build lock and a temporary context containing only the
Dockerfile, Python constraints, and runtime verification script. Credentials and
task outputs never enter that context.

The Dockerfile also supports the framework's normal `src/` build context. Direct
Docker builds default to its tested Omega commit; pass
`--build-arg OMEGA_REVISION=COMMIT` to select another revision. The image includes
the local embedding model so runtime initialization can work offline. The optional MORK
Rust extension is absent, matching the existing Omega account image; PeTTa,
FAISS, and the Python bridge are available.

For authentication commands inside the agent image:

```bash
python3 /PeTTa/repos/Omega/scripts/chatgpt-auth status
```

## Run with the environment controller

From the envtraverse directory:

```bash
python3 run_controller_omega.py --run-config run_config_omega.yaml
```

The controller reads `omega_accounts` instead of `codex_accounts` and requires
`agent: omega_agent_v1`. It builds the agent, selected environment server, and
model proxy; checks each account with one small inference using that run's image;
and schedules one task at a time per account. Authentication directories are
mounted **in place, read/write**, so refreshed credentials survive. They are
never copied into the run folder or a Docker build context, and no account
cleaning step runs.

The example configuration uses `environment: example`, `games: [counter]`,
`omega_accounts: [omega01]`, and `server_terminal_grace_seconds: 10`. After the
server finishes a terminal response, it waits ten seconds and exits. The shared
scheduler then stops the remaining Omega container. This is lifecycle handling,
not an agent recovery supervisor or automatic continuation mechanism.

Agent traffic uses the same internal Docker network and OpenAI allowlisted proxy
as the Codex controller. The local embedding model loads offline. Omega uses a
16 GiB container memory cap and 8 GiB soft/hard process-data limits by default,
with no additional swap. These limits apply to agent and account-check containers.
The example `run_config_omega.yaml` sets all three `runner` memory settings
explicitly. Partial overrides retain Omega's defaults for unspecified values.

Outputs are written to `<run-root>/run/`; an existing run folder is refused.
To keep another experiment's outputs separately while using the same account:

```bash
python3 run_controller_omega.py --run-config run_config_omega.yaml \
  --run-root /path/to/fresh-experiment \
  --omega-accounts-dir /absolute/path/to/envtraverse_v0.1/omega_accounts
```

Persistent logs include:

- `run/runner.log`: scheduling, game aliases, image identities, and shutdown.
- `run/account_checks/<account>.log`: account preflight output.
- `run/<alias>/run/omega_console.log`: full agent stdout/stderr, including startup errors.
- `run/<alias>/run/agent.log`: Omega runtime events and `[LLM_USAGE]` records.
- `run/<alias>/server/server.log`: environment activity and terminal grace shutdown.

Each completed agent response logs `input_tokens`, `cached_tokens`,
`output_tokens`, and `reasoning_tokens` when the API supplies them. Cached tokens
are included in input tokens; reasoning tokens are included in output tokens.
These are response usage records, not account balance or monetary cost. Failed
or interrupted responses can lack usage records, including an in-flight call
stopped when the terminal grace period ends.

## Manual launch against an existing server

Start an environment server separately, then run this from the envtraverse
directory on the same Linux Docker host:

```bash
python3 src/agents/omega_agent_v1/launch.py \
  --server-url http://127.0.0.1:8000 \
  --game TASK_ALIAS \
  --account ./omega_accounts/omega01 \
  --run-dir ./run/omega-first-test \
  --model MODEL_ID \
  --reasoning-effort medium
```

Replace the server URL, alias, account path, and model with the values for the
test. The output directory must not already exist. The launcher prepares public
task files, mounts the account directory read/write, and replaces itself with
`docker run --init`. Signals are forwarded to the whole Omega process group,
so Ctrl+C also reaches SWI-Prolog through PeTTa's shell launcher. It is not an agent supervisor. Linux host networking makes
the local server reachable. This manual launcher does not apply the standard
experiment controller's model-proxy/network-isolation setup.

Optional arguments:

- `--initial-files PATH` copies public environment files into the workspace.
- `--max-new-input-loops N` changes the active cycle count (default `10000000`).
- `--http-read-timeout-seconds N` configures the unchanged client (default `3900`).

`prepare_run.py` also implements the framework's standard `LaunchPreparation`
hook. Use `run_controller_omega.py` for Omega accounts. The original Codex subscription
controllers continue to use Codex account authentication.

## Runtime and files

`agent.py` starts the session with the existing `/client/client.py`, verifies that
the server wrote `/observations/environment_description.md`, and constructs the
initial prompt from `main_prompt.md` and the initial client output. A local
channel delivers that prompt once, then returns no new input. Omega continues
using its existing autonomous loop.

The `agent/client` files are copied unchanged from `agent_v1`. All initial task
instructions, including when to stop, are kept in `main_prompt.md`.

The task output directory contains:

```text
client/                  unchanged client, configuration, and session state
observations/            environment descriptions, action records, observations
run/
  agent.py               launcher executed inside the container
  task_prompt.txt        the single prompt delivered to Omega
  agent.log              Omega runtime log
  omega-policy.json      generated filesystem policy
  agent_run/             writable task workspace
omega_memory/            fresh stock prompts, history, and Chroma data
```

The actual Omega memory directory is mounted per task. Authentication is a
separate mount and contains no experiment history. Shell commands run in
`/home/user/run/agent_run`; its reserved `repos` symlink points to the installed
PeTTa repositories so Omega's existing imports and history lookup work there.
Initial workspace files must not contain a top-level `repos` path.

The client and observation mounts are writable because the unchanged client
temporarily changes file permissions to persist server responses. The prompt
instructs the agent not to modify observation files directly, matching `agent_v1`.

## Current limits

- Omega's built-in shell timeout remains **five seconds**. It also applies when
  the model invokes the client through shell, regardless of the client's HTTP
  timeout. Long environment actions or scientific jobs can be interrupted.
- There is no image-viewing tool or multimodal observation adapter in this
  version; use tasks whose observations Omega can inspect as text or through
  workspace code.
- The original prompt is delivered once and can eventually leave Omega's
  rolling history. This version does not add persistent goal injection.
- The stock Omega prompt still requests memory queries and several cycles before
  decisions. It is not rewritten for benchmark behavior.
- Idle/heartbeat logs do not prove task progress, and process exit does not prove
  success. Inspect the environment's state when evaluating the run.

## Offline validation

```bash
python3 -m unittest discover -s src/agents/omega_agent_v1/tests -v
python3 -m unittest discover -s tests -p test_omega_controller.py -v
python3 src/agents/omega_agent_v1/tests/smoke_native_runtime.py
python3 src/agents/omega_agent_v1/tests/smoke_agent.py
```

The smoke checks require the built Docker image. Set `OMEGA_SMOKE_IMAGE` to test
another tag. They exercise scientific computations through Python, Prolog, and
MeTTa, then a full Omega run against a temporary local environment server and
mocked model responses. They verify one initial prompt, an action through the
unchanged client, persisted history, and idle behavior after two model cycles.
They print temporary artifact paths and remove their containers on completion.
These tests do not use saved accounts or perform live model requests.
