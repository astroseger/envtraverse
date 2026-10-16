#!/usr/bin/env python3
"""Minimal LLM agent demo (OpenAI-compatible / OpenRouter).

This is a *demo* of how to wire an agent against the WebUI API:
- fetches game description (i18n)
- starts a task (TRAINING by default)
- loops for up to max_steps, calling an LLM to choose an action
- executes the action via /api/step

It uses OpenAI Chat Completions with tool-calling. OpenRouter supports a compatible API.

Environment variables:
- OPENAI_API_KEY or OPENROUTER_API_KEY
- OPENAI_BASE_URL or OPENROUTER_BASE_URL (optional)

Examples:
  OPENROUTER_API_KEY=... python3 agents/demo_llm_agent_openrouter.py --base-url http://127.0.0.1:8000 \
    --path TRAINING/S0-keys/STAGE-01 --lang en --model openrouter/auto

  OPENROUTER_API_KEY=... python3 agents/demo_llm_agent_openrouter.py --base-url http://127.0.0.1:8000 \
    --path CLASSIC/EASY/pits/4x4/0000.json --lang en --model openrouter/auto

  OPENAI_API_KEY=... OPENAI_BASE_URL=https://openrouter.ai/api/v1 \
    python3 agents/demo_llm_agent_openrouter.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from urllib.error import HTTPError, URLError
import socket
import random


def http_get_json(url: str) -> dict:
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read().decode("utf-8"))


def http_post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        # Preserve JSON error payloads (e.g. bad_action) as a normal response.
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            raise


def openai_chat_completions(
    *,
    base_url: str,
    api_key: str,
    payload: dict,
    timeout_s: float = 60.0,
    retries: int = 5,
    backoff_s: float = 0.8,
) -> dict:
    """Call OpenAI-compatible Chat Completions with simple retries.

    Retries are meant for transient network/endpoint issues (timeouts, disconnects, 502/503/504).
    """

    url = base_url.rstrip("/") + "/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    def _sleep(attempt: int) -> None:
        # exponential backoff with small jitter
        t = backoff_s * (2 ** max(0, attempt - 1))
        t = t * (0.8 + 0.4 * random.random())
        time.sleep(t)

    last_err: Exception | None = None
    for attempt in range(1, int(retries) + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            # Retry only for transient server/proxy errors.
            if e.code in (502, 503, 504) and attempt < retries:
                last_err = e
                _sleep(attempt)
                continue
            raise
        except (URLError, socket.timeout, TimeoutError) as e:
            if attempt < retries:
                last_err = e
                _sleep(attempt)
                continue
            raise

    # Shouldn't happen, but keep a clear failure mode.
    raise RuntimeError(f"openai_chat_completions failed after {retries} retries: {last_err}")


def find_start_in_public_map(map_txt: str) -> tuple[int, int] | None:
    # public map shows cells as " x " inside a grid. We'll just locate 'S' in text.
    lines = map_txt.splitlines()
    for r, line in enumerate(lines):
        c = line.find("S")
        if c != -1:
            return (r, c)
    return None


def run_agent(
    *,
    base_url: str = "http://127.0.0.1:8000",
    path: str = "TRAINING/S0-keys/STAGE-01",
    lang: str = "en",
    seed: int | None = None,
    model: str = "openrouter/auto",
    max_steps: int | None = None,
    verbose: bool = False,
) -> dict:
    """Run the LLM agent for one episode.

    Returns a dict suitable for JSON logging.
    """

    args = argparse.Namespace(
        base_url=base_url,
        path=path,
        lang=lang,
        seed=seed,
        model=model,
        max_steps=max_steps,
        verbose=verbose,
    )
    return _run_agent_impl(args)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", type=str, default="http://127.0.0.1:8000", help="Labyrinth WebUI server")
    ap.add_argument(
        "--path",
        type=str,
        default="TRAINING/S0-keys/STAGE-01",
        help=(
            "PACKS-relative path including group, e.g. TRAINING/S0-keys/STAGE-01, "
            "CLASSIC/EASY/pits/4x4, EXTENDED/boat/STAGE-01-4x4, or a file like CLASSIC/.../0000.json"
        ),
    )
    ap.add_argument("--lang", type=str, default="en", choices=("en", "ru"))
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--model", type=str, default="openrouter/auto")
    ap.add_argument("--max-steps", type=int, default=None, help="override task max_steps")
    ap.add_argument(
        "--verbose",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="print server messages (default: enabled; use --no-verbose to silence)",
    )
    cli_args = ap.parse_args()

    res = run_agent(
        base_url=cli_args.base_url,
        path=cli_args.path,
        lang=cli_args.lang,
        seed=cli_args.seed,
        model=cli_args.model,
        max_steps=cli_args.max_steps,
        verbose=bool(cli_args.verbose),
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))


# --- old main body moved below into _run_agent_impl() ---

def _run_agent_impl(args) -> dict:
    """Internal implementation; expects an argparse-like args."""

    # LLM endpoint/key
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("Missing OPENAI_API_KEY or OPENROUTER_API_KEY")

    base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1"

    srv = args.base_url.rstrip("/")

    desc = http_get_json(f"{srv}/api/description?lang={args.lang}").get("description", "")

    # Start game
    start_payload = {
        "path": args.path,
        "lang": args.lang,
        "seed": args.seed,
        "client": "api",
        "agent": "demo_llm_openrouter",
    }

    if getattr(args, "verbose", False):
        print(f"Starting game: path={args.path}")

    start = http_post_json(f"{srv}/api/start", start_payload)
    sid = start["id"]
    info = start.get("info")
    task_meta = start.get("task_meta") or {}
    public_map = start.get("map", "")
    actions = start.get("actions") or ["up", "down", "left", "right"]

    if getattr(args, "verbose", False) and start.get("text"):
        print("START:", start.get("text"))
    if getattr(args, "verbose", False) and info:
        print("INFO:", info)

    max_steps = args.max_steps
    if max_steps is None:
        ms = task_meta.get("max_steps")
        max_steps = int(ms) if isinstance(ms, int) else 200

    # System prompt
    info_str = ""
    if info:
        info_str = f"info: rivers={info.get('rivers')} pits={info.get('pits_cycles')}x{info.get('pits_cycle_len')}"

    board_str = ""
    if task_meta.get("board"):
        b = task_meta["board"]
        board_str = f"board size: n={b.get('n')} rows, m={b.get('m')} cols"

    start_pos_str = ""
    if task_meta.get("start"):
        s0 = task_meta["start"]
        start_pos_str = f"start position (row,col): ({s0.get('row')},{s0.get('col')})"

    sys_prompt = (
        "You are playing a grid labyrinth game. Your goal is to win the game by collecting the treasure and exiting.\n\n"
        "Rules/description:\n" + desc + "\n\n"
        "Task info:\n" + info_str + "\n" + board_str + "\n" + start_pos_str + "\n\n"
        "Map (public):\n" + public_map + "\n\n"
        "Coordinate convention: positions are given as (row, col), where row increases downward and col increases to the right.\n\n"
        "You will receive the result of each move as text and a structured 'formal' object (no coordinates).\n"
        "Choose one action each step. Avoid wasting moves."
    )

    tool = {
        "type": "function",
        "function": {
            "name": "act",
            "description": "Choose the next action",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(actions)},
                    "reason": {"type": "string"},
                },
                "required": ["action"],
            },
        },
    }

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": "Game started. Choose your first action."},
    ]

    def choose_action() -> str:
        payload = {
            "model": args.model,
            "messages": messages,
            "tools": [tool],
            "tool_choice": {"type": "function", "function": {"name": "act"}},
        }
        resp = openai_chat_completions(base_url=base_url, api_key=api_key, payload=payload)
        choice = resp["choices"][0]
        msg = choice["message"]

        # tool call
        tcs = msg.get("tool_calls") or []
        if tcs:
            args_json = tcs[0]["function"]["arguments"]
            data = json.loads(args_json) if isinstance(args_json, str) else args_json
            return str(data["action"])

        # fallback: parse plain text
        txt = (msg.get("content") or "").strip().lower()
        for a in actions:
            if a in txt:
                return a
        return random.choice(list(actions))

    import random

    # run loop
    done = False
    steps = 0
    last_inv = start.get("inventory")

    for step in range(1, max_steps + 1):
        steps = step
        action = choose_action()
        out = http_post_json(f"{srv}/api/step", {"id": sid, "action": action, "seq": step})

        text = out.get("text", "")
        formal = out.get("formal")
        # Handle server-side errors (e.g. bad_action) without crashing the agent.
        if out.get("error"):
            err = out.get("error")
            if getattr(args, "verbose", False):
                print(f"[Step {step}] action={action} SERVER_ERROR={err} payload={json.dumps(out, ensure_ascii=False)}")
            # Feed error back to the model so it can adapt.
            messages.append({"role": "assistant", "content": f"I choose: {action}"})
            messages.append({"role": "user", "content": f"Server error: {json.dumps(out, ensure_ascii=False)}"})
            # Don't count as done; continue.
            continue

        done = bool(out.get("done"))
        last_inv = out.get("inventory")

        if getattr(args, "verbose", False):
            print(f"[Step {step}] action={action} text={text}")
            if formal:
                print("  formal=", json.dumps(formal, ensure_ascii=False))

        # Append to conversation
        messages.append({"role": "assistant", "content": f"I choose: {action}"})
        messages.append({"role": "user", "content": f"Result: {text}\nformal: {json.dumps(formal, ensure_ascii=False)}"})

        if done:
            if getattr(args, "verbose", False):
                print("WIN")
            break

    return {
        "success": bool(done),
        "steps": int(steps),
        "inventory": last_inv,
        "session_id": sid,
    }


if __name__ == "__main__":
    main()
