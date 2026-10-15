# Labyrinth Agents (baseline)

This folder contains **baseline agents** for the *AGI Labyrinth benchmark*.

These agents are intentionally simple and are meant for:
- **API smoke-testing** (is the server working?)
- **calibration** (what success rate is achievable with trivial / naive policies?)
- **benchmark harness validation** (logs, repeats, aggregation)

They are *not* necessarily strong solvers.

## What’s in here

### Baseline agents

- `demo_random_agent.py`
  - Random policy.
  - Useful as a lower-bound and for server/API load testing.

- `demo_simplest_llm_agent_openrouter.py`
  - Minimal LLM agent (OpenAI-compatible / OpenRouter).
  - Uses tool-calling to pick an action.

- `demo_llm_agent_openrouter.py`
  - Slightly richer LLM demo (includes more structured feedback).

- `demo_planning_llm_agent_openrouter.py`
  - Two-phase LLM loop (PLAN then ACT) + loop-avoidance nudge.

All agents expose `run_agent(...) -> dict` for programmatic use by the benchmark runner,
while still supporting CLI execution.

### Benchmark utilities

- `benchmark_runner.py`
  - Runs an agent over a list of PACKS paths (files or folders).
  - Writes **JSONL** logs (one episode per line).
  - Supports `--repeats`.

- `bench_report.py`
  - Reads JSONL logs and prints success rates.
  - Can split successes into `good` vs `late` using `--good-steps`.

## Common conventions

### Paths

The benchmark uses **PACKS-relative** paths that include the group as the first segment.
Examples:

- `TRAINING/S0-keys/STAGE-01/0005.json`
- `CLASSIC/EASY/pits/4x4/0000.json`
- `EXTENDED/boat/STAGE-01-4x4/0012.json`

You can also pass a directory (then all `*.json` inside are used).

### Server

These agents expect the WebUI server to be running, e.g.:

```bash
python3 scripts/webui_server.py --host 127.0.0.1 --port 8000
```

### LLM environment

LLM agents require:
- `OPENAI_API_KEY` or `OPENROUTER_API_KEY`
- Optional: `OPENAI_BASE_URL` / `OPENROUTER_BASE_URL`

## Running examples

Random agent:

```bash
python3 agents/demo_random_agent.py --base-url http://127.0.0.1:8000 \
  --path CLASSIC/EASY/pits/4x4/0000.json
```

Benchmark run:

```bash
python3 agents/benchmark_runner.py --base-url http://127.0.0.1:8000 \
  --agent random \
  --path TRAINING/S0-keys/STAGE-01 \
  --repeats 3 \
  --out logs/bench_random_r3.jsonl
```

Report:

```bash
python3 agents/bench_report.py logs/bench_random_r3.jsonl
python3 agents/bench_report.py logs/bench_random_r3.jsonl --good-steps 200
```

## Results template (fill-in)

> Keep results reproducible: record commit hash, model, server config, and paths.

### Environment

- Repo commit: `<hash>`
- Server: `scripts/webui_server.py` (port `<port>`, debug=`<true|false>`)
- Agent: `<random|simplest_llm|llm|planning_llm>`
- Model (if LLM): `<openrouter/...>`
- Command line:
  ```bash
  <paste the exact command>
  ```

### Aggregate metrics

- Episodes: `<N>`
- Success rate: `<%>`
- Success rate (good, <=K steps): `<%>` (K=`<K>`)
- Success rate (late, >K steps): `<%>`
- Mean steps on success: `<float>`

### Breakdown

- By group:
  - TRAINING: `<%>`
  - CLASSIC: `<%>`
  - EXTENDED: `<%>`
  - TUTORIAL: `<%>`

- By level (depth=`<d>`):
  - `<path-prefix>`: `<%>`

---

If you add a new agent, please:
1) export `run_agent(...) -> dict`
2) make it robust to server-side JSON errors (e.g. `bad_action`)
3) document it here
