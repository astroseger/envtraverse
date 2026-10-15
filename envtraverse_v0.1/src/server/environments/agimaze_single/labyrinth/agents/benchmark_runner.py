#!/usr/bin/env python3
"""Benchmark/auto-test runner for agents.

Runs an agent over a set of PACKS paths (files or folders) and logs results.

Examples:
  python3 agents/benchmark_runner.py --base-url http://127.0.0.1:8000 \
    --agent random --path CLASSIC/EASY/pits/4x4 --out logs/bench_random.jsonl

  python3 agents/benchmark_runner.py --base-url http://127.0.0.1:8000 \
    --agent planning_llm --path CLASSIC/EASY/pits/4x4/0000.json --model openrouter/auto

Notes:
- Paths are PACKS-relative and must include the group as the first segment.
- If a --path is a directory, we expand it to all *.json files inside (sorted).
- Logging is JSONL: one record per episode.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "packs"

# Allow importing agent modules from this repo when running as a script.
sys.path.insert(0, str(ROOT))


AGENT_MODULES = {
    "random": "demo_random_agent",
    "llm": "demo_llm_agent_openrouter",
    "simplest_llm": "demo_simplest_llm_agent_openrouter",
    "planning_llm": "demo_planning_llm_agent_openrouter",
}


def _utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expand_path(p: str) -> list[str]:
    """Expand a PACKS-relative path into a list of PACKS-relative json files.

    - If p points to a file: return [p]
    - If p points to a directory: return all json files under that directory (non-recursive)
    """

    rel = p.strip().lstrip("/").replace("\\", "/")
    if ".." in rel.split("/"):
        raise ValueError(f"bad_path: {p}")

    fs = PACKS / rel
    if not fs.exists():
        raise FileNotFoundError(f"not_found: {p}")

    if fs.is_file():
        return [rel]

    # dir
    files = sorted([x for x in fs.glob("*.json") if x.is_file()])
    return [str(x.relative_to(PACKS)).replace("\\", "/") for x in files]


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", type=str, default="http://127.0.0.1:8000")
    ap.add_argument("--agent", type=str, default="random", choices=sorted(AGENT_MODULES.keys()))
    ap.add_argument(
        "--path",
        type=str,
        action="append",
        required=True,
        help="PACKS-relative path (file or folder) including group prefix. Repeatable.",
    )
    ap.add_argument("--lang", type=str, default="en", choices=("en", "ru"))
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--max-steps", type=int, default=None)
    ap.add_argument("--model", type=str, default=None, help="for LLM agents")
    ap.add_argument("--temperature", type=float, default=None, help="for planning_llm")
    ap.add_argument("--out", type=str, default="logs/bench.jsonl")
    ap.add_argument("--fail-fast", action="store_true")
    ap.add_argument("--verbose", action="store_true", help="pass verbose=true to agent")
    ap.add_argument("--repeats", type=int, default=1, help="How many times to run each target (default: 1)")
    args = ap.parse_args()

    if args.repeats < 1:
        raise SystemExit("--repeats must be >= 1")

    out_path = (ROOT / args.out).resolve() if not os.path.isabs(args.out) else Path(args.out)
    _ensure_parent(out_path)

    mod_name = AGENT_MODULES[args.agent]
    mod = importlib.import_module(mod_name)
    if not hasattr(mod, "run_agent"):
        raise SystemExit(f"Agent module {mod_name} does not export run_agent(...)")

    run_agent = getattr(mod, "run_agent")

    # Expand all requested paths
    targets: list[str] = []
    for p in args.path:
        targets.extend(_expand_path(p))

    targets = sorted(dict.fromkeys(targets))  # stable unique

    total_eps = len(targets) * int(args.repeats)
    print(f"agent={args.agent} targets={len(targets)} repeats={args.repeats} episodes={total_eps} out={out_path}")

    with out_path.open("a", encoding="utf-8") as f:
        idx = 0
        for rel in targets:
            for r in range(1, int(args.repeats) + 1):
                idx += 1
                payload: dict[str, Any] = {
                    "base_url": args.base_url,
                    "path": rel,
                    "lang": args.lang,
                    "seed": args.seed,
                    "max_steps": args.max_steps,
                    "verbose": bool(args.verbose),
                }
                if args.model is not None:
                    payload["model"] = args.model
                if args.temperature is not None:
                    payload["temperature"] = args.temperature

                rec: dict[str, Any] = {
                    "ts": _utc_ts(),
                    "idx": idx,
                    "episodes": total_eps,
                    "agent": args.agent,
                    "path": rel,
                    "lang": args.lang,
                    "seed": args.seed,
                    "repeat": r,
                    "repeats": int(args.repeats),
                }

                try:
                    res = run_agent(**payload)
                    if not isinstance(res, dict):
                        res = {"result": res}
                    rec.update(res)
                    ok = bool(rec.get("success"))
                    steps = rec.get("steps")
                    print(f"[{idx:04d}/{total_eps:04d}] {'OK ' if ok else 'FAIL'} rep={r}/{args.repeats} steps={steps} {rel}")
                except Exception as e:
                    rec.update({"success": False, "error": repr(e)})
                    print(f"[{idx:04d}/{total_eps:04d}] ERROR rep={r}/{args.repeats} {rel}: {e}")
                    if args.fail_fast:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        f.flush()
                        raise

                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()


if __name__ == "__main__":
    main()
