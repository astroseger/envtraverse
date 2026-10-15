#!/usr/bin/env python3
"""Demo/mock agent for the Labyrinth WebUI API.

It:
- queries available training series/stages
- picks a random (series, stage)
- starts a session
- performs random moves for N steps (or until done)
- prints observations/messages it receives

This is *not* a solver. It's just an API smoke-test.

Usage:
  python3 agents/demo_random_agent.py --base-url http://127.0.0.1:8000 --steps 50 --lang en

Notes:
- Uses only standard library.
- Assumes the WebUI server is running.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.request


def http_get(url: str) -> dict:
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read().decode("utf-8"))


def http_post(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode("utf-8"))


def run_agent(
    *,
    base_url: str = "http://127.0.0.1:8000",
    path: str | None = None,
    lang: str = "en",
    seed: int | None = None,
    max_steps: int | None = None,
    agent: str = "demo_random",
    verbose: bool = False,
) -> dict:
    """Run the random agent for one episode.

    Returns a dict suitable for JSON logging.
    """

    base = base_url.rstrip("/")
    # Step limit resolution:
    # - if max_steps is explicitly provided: use it
    # - otherwise: try task_meta.max_steps from the server
    # - fallback: 50
    steps_limit = int(max_steps) if isinstance(max_steps, int) and max_steps > 0 else None

    if path:
        start_payload = {
            "path": path,
            "lang": lang,
            "seed": seed,
            "client": "api",
            "agent": agent,
        }
        if verbose:
            print(f"Starting game: path={path}")
    else:
        # Legacy behavior: pick a random TRAINING series/stage.
        tr = http_get(base + "/api/training")
        series = tr.get("series", [])
        if not series:
            raise RuntimeError("No TRAINING series found")

        s = random.choice(series)
        stages = (tr.get("stages", {}) or {}).get(s, [])
        if not stages:
            raise RuntimeError(f"No stages found for series {s}")

        st = random.choice(stages)

        start_payload = {
            "group": "TRAINING",
            "series": s,
            "stage": st,
            "lang": lang,
            "seed": seed,
            "client": "api",
            "agent": agent,
        }
        if verbose:
            print(f"Starting game: TRAINING/{s}/{st}")

    t0 = time.time()
    resp = http_post(base + "/api/start", start_payload)
    sid = resp["id"]

    if verbose and resp.get("text"):
        print("START:", resp.get("text"))
    if verbose and resp.get("info"):
        print("INFO:", resp.get("info"))

    if steps_limit is None:
        task_meta = resp.get("task_meta") or {}
        ms = task_meta.get("max_steps")
        steps_limit = int(ms) if isinstance(ms, int) and ms > 0 else 50

    actions = resp.get("actions") or ["up", "down", "left", "right"]

    last_inv = resp.get("inventory")
    done = False
    steps = 0

    for i in range(1, steps_limit + 1):
        steps = i
        a = random.choice(list(actions))
        out = http_post(base + "/api/step", {"id": sid, "action": a, "seq": i})
        txt = out.get("text", "")
        last_inv = out.get("inventory")
        formal = out.get("formal")
        done = bool(out.get("done"))
        if verbose:
            print(f"[{i:03d}] {a:>5} | {txt} | inv={last_inv}")
            if formal is not None:
                print("       formal:", json.dumps(formal, ensure_ascii=False))
        if done:
            if verbose:
                print("DONE")
            break

    return {
        "success": bool(done),
        "steps": int(steps),
        "inventory": last_inv,
        "session_id": sid,
        "elapsed_s": time.time() - t0,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", type=str, default="http://127.0.0.1:8000")
    ap.add_argument("--steps", type=int, default=50)
    ap.add_argument("--lang", type=str, default="en", choices=("en", "ru"))
    ap.add_argument("--seed", type=int, default=None, help="optional rng.seed override for the task")
    ap.add_argument(
        "--path",
        type=str,
        default=None,
        help=(
            "Optional PACKS-relative path including group, e.g. TRAINING/S0-keys/STAGE-01, "
            "CLASSIC/EASY/pits/4x4, EXTENDED/boat/STAGE-01-4x4, or a file like CLASSIC/.../0000.json. "
            "If not provided, the agent will pick a random TRAINING series/stage (legacy behavior)."
        ),
    )
    ap.add_argument("--agent", type=str, default="demo_random", help="agent name for api_wins.log")
    ap.add_argument(
        "--verbose",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="print server messages (default: enabled; use --no-verbose to silence)",
    )
    args = ap.parse_args()

    res = run_agent(
        base_url=args.base_url,
        path=args.path,
        lang=args.lang,
        seed=args.seed,
        max_steps=args.steps,
        agent=args.agent,
        verbose=bool(args.verbose),
    )

    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
