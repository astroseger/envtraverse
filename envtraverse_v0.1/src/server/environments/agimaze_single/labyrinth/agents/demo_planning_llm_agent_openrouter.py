#!/usr/bin/env python3
"""More capable LLM agent demo (OpenRouter/OpenAI-compatible).

Compared to demo_llm_agent_openrouter.py:
- does NOT include the full `formal` JSON every step (to avoid confusing the model)
- uses a 2-phase loop: PLAN (free text) then ACT (tool call)
- provides a richer tool description and explicit exploration guidance
- injects a "you are looping" nudge if it detects repeating patterns

Environment:
- OPENAI_API_KEY or OPENROUTER_API_KEY
- OPENAI_BASE_URL or OPENROUTER_BASE_URL (optional; defaults to OpenRouter)

Usage:
  OPENROUTER_API_KEY=... python3 agents/demo_planning_llm_agent_openrouter.py \
    --base-url http://127.0.0.1:8000 --path TRAINING/S0-keys/STAGE-01 --lang en --model openrouter/auto
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import urllib.request
from urllib.error import HTTPError, URLError
import socket


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
    """Call OpenAI-compatible Chat Completions with simple retries."""

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
        t = backoff_s * (2 ** max(0, attempt - 1))
        t = t * (0.8 + 0.4 * random.random())
        time.sleep(t)

    last_err: Exception | None = None
    for attempt in range(1, int(retries) + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
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

    raise RuntimeError(f"openai_chat_completions failed after {retries} retries: {last_err}")


def extract_compact_signal(step_resp: dict) -> str:
    """Extract a compact, low-spoiler signal for the model."""
    formal = step_resp.get("formal") or {}
    status = formal.get("status")
    events = formal.get("events") or []
    details = formal.get("details") or {}

    landed = (details.get("landed") or {}).get("kind")

    river = details.get("river")
    river_s = ""
    if isinstance(river, dict):
        if river.get("carried"):
            river_s = f" river:carried steps={river.get('steps')} to_mouth={river.get('to_mouth')}"
        else:
            river_s = " river:entered"

    # Inventory is safe (no coordinates)
    inv = (formal.get("inventory") or step_resp.get("inventory") or {})

    return f"status={status} landed={landed} events={events}{river_s} inv={inv}"


def run_agent(
    *,
    base_url: str = "http://127.0.0.1:8000",
    path: str = "TRAINING/S0-keys/STAGE-01",
    lang: str = "en",
    seed: int | None = None,
    model: str = "openrouter/auto",
    max_steps: int | None = None,
    temperature: float = 0.2,
    verbose: bool = False,
) -> dict:
    args = argparse.Namespace(
        base_url=base_url,
        path=path,
        lang=lang,
        seed=seed,
        model=model,
        max_steps=max_steps,
        temperature=temperature,
        verbose=verbose,
    )
    return _run_agent_impl(args)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", type=str, default="http://127.0.0.1:8000")
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
    ap.add_argument("--max-steps", type=int, default=None)
    ap.add_argument("--temperature", type=float, default=0.2)
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
        temperature=cli_args.temperature,
        verbose=bool(cli_args.verbose),
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))


def _run_agent_impl(args) -> dict:

    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("Missing OPENAI_API_KEY or OPENROUTER_API_KEY")

    base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1"

    srv = args.base_url.rstrip("/")

    desc = http_get_json(f"{srv}/api/description?lang={args.lang}").get("description", "")

    start_payload = {
        "path": args.path,
        "lang": args.lang,
        "seed": args.seed,
        "client": "api",
        "agent": "demo_planning_llm_openrouter",
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
        "You are an agent solving a hidden-map labyrinth puzzle. Your goal is to WIN.\n\n"
        "Rules/description:\n" + desc + "\n\n"
        "Non-spoiler task info:\n" + info_str + "\n" + board_str + "\n" + start_pos_str + "\n\n"
        "Public map (geometry only):\n" + public_map + "\n\n"
        "Coordinate convention: (row,col), row increases downward, col increases to the right.\n\n"
        "Important guidance:\n"
        "- You cannot see the full maze. Treat this as exploration.\n"
        "- If a move is blocked, that direction is not passable from your current cell. Try another direction.\n"
        "- Avoid loops: keep track of what you just tried and vary your exploration.\n"
        "- You must obtain the key, open the chest to take the treasure, then exit.\n"
        "- Prefer systematic exploration (e.g., wall-following or graph exploration) over repeating two moves.\n"
        "\n"
        "You will act by calling the tool `act` with one allowed action."
    )

    tool = {
        "type": "function",
        "function": {
            "name": "act",
            "description": (
                "Choose the next move action.\n"
                "Actions are grid moves: up/down/left/right.\n"
                "A blocked result means a wall/monolith prevents movement in that direction from your current cell.\n"
                "Your objective is to explore, obtain key -> open chest -> take treasure -> exit.\n"
                "Avoid repeating the same action pattern if it doesn't make progress."
            ),
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

    messages: list[dict] = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": "Game started. Provide an initial plan, then choose an action."},
    ]

    def llm_plan() -> str:
        payload = {
            "model": args.model,
            "temperature": args.temperature,
            "messages": messages + [{"role": "user", "content": "PLAN: make notes for yourself what tiles have been explored and what actions should be done. Use your answer to this message as your working memory."}],
        }
        resp = openai_chat_completions(base_url=base_url, api_key=api_key, payload=payload)
        msg = resp["choices"][0]["message"]
        txt = (msg.get("content") or "").strip()
        return txt[:1200]

    def llm_act() -> tuple[str, str]:
        payload = {
            "model": args.model,
            "temperature": args.temperature,
            "messages": messages,
            "tools": [tool],
            "tool_choice": {"type": "function", "function": {"name": "act"}},
        }
        resp = openai_chat_completions(base_url=base_url, api_key=api_key, payload=payload)
        msg = resp["choices"][0]["message"]
        tcs = msg.get("tool_calls") or []
        if tcs:
            args_json = tcs[0]["function"]["arguments"]
            data = json.loads(args_json) if isinstance(args_json, str) else args_json
            return str(data["action"]), str(data.get("reason", ""))
        # fallback
        txt = (msg.get("content") or "").lower()
        for a in actions:
            if a in txt:
                return a, "(parsed from text)"
        return random.choice(list(actions)), "(random fallback)"

    # crude loop detection: last few (action,status,landed)
    hist_sig: list[tuple[str, str, str]] = []

    done = False
    steps = 0
    last_inv = start.get("inventory")

    for step in range(1, max_steps + 1):
        steps = step
        # PLAN phase
        plan = llm_plan()
        if plan:
            messages.append({"role": "assistant", "content": "NOTES: " + plan})

        # Loop nudge
        if len(hist_sig) >= 6 and len(set(hist_sig[-6:])) <= 2:
            messages.append(
                {
                    "role": "user",
                    "content": "You seem to be looping. Change strategy: explore a different direction and avoid repeating the last pattern.",
                }
            )

        # ACT phase
        action, reason = llm_act()
        out = http_post_json(f"{srv}/api/step", {"id": sid, "action": action, "seq": step})

        text = out.get("text", "")
        # Handle server-side errors (e.g. bad_action) without crashing the agent.
        if out.get("error"):
            err = out.get("error")
            if getattr(args, "verbose", False):
                print(f"[Step {step}] action={action} SERVER_ERROR={err} payload={json.dumps(out, ensure_ascii=False)}")
            messages.append({"role": "assistant", "content": f"I choose action: {action}. Reason: {reason}"})
            messages.append({"role": "user", "content": f"Server error: {json.dumps(out, ensure_ascii=False)}"})
            continue

        done = bool(out.get("done"))
        last_inv = out.get("inventory")
        signal = extract_compact_signal(out)

        if getattr(args, "verbose", False):
            print(f"[Step {step}] action={action} reason={reason}\n  text={text}\n  {signal}")

        # Update history signature
        formal = out.get("formal") or {}
        details = formal.get("details") or {}
        landed = (details.get("landed") or {}).get("kind")
        hist_sig.append((action, str(formal.get("status")), str(landed)))

        # Feed back to model (no raw formal dump)
        messages.append({"role": "assistant", "content": f"I choose action: {action}. Reason: {reason}"})
        messages.append({"role": "user", "content": f"Result text: {text}\nSignal: {signal}"})

        if done:
            break

    return {
        "success": bool(done),
        "steps": int(steps),
        "inventory": last_inv,
        "session_id": sid,
    }


if __name__ == "__main__":
    main()
