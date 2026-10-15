#!/usr/bin/env python3
"""Benchmark report helper.

Reads JSONL produced by agents/benchmark_runner.py and prints summary stats:
- total success rate
- per-group success rate (TRAINING/CLASSIC/EXTENDED/TUTORIAL)
- per-level success rate by folder path (configurable depth)

Examples:
  python3 agents/bench_report.py logs/bench_random.jsonl
  python3 agents/bench_report.py logs/bench_random.jsonl --by-depth 3
  python3 agents/bench_report.py logs/bench_random.jsonl --group-by group

By default, "level" is inferred from the path prefix with depth=2:
  TRAINING/S0-keys/STAGE-01/0005.json -> TRAINING/S0-keys
  CLASSIC/EASY/pits/4x4/0000.json     -> CLASSIC/EASY

This keeps the default report compact while still being useful.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Agg:
    n: int = 0
    ok: int = 0
    ok_good: int = 0
    ok_late: int = 0

    steps_ok_sum: int = 0
    steps_ok_n: int = 0

    def add(self, *, success: bool, steps: int | None, good_steps: int | None) -> None:
        self.n += 1
        if success:
            self.ok += 1
            if isinstance(steps, int) and steps >= 0:
                self.steps_ok_sum += int(steps)
                self.steps_ok_n += 1
            if good_steps is not None and isinstance(steps, int):
                if steps <= int(good_steps):
                    self.ok_good += 1
                else:
                    self.ok_late += 1

    def rate(self) -> float:
        return (self.ok / self.n) if self.n else 0.0

    def rate_good(self) -> float:
        return (self.ok_good / self.n) if self.n else 0.0

    def rate_late(self) -> float:
        return (self.ok_late / self.n) if self.n else 0.0

    def mean_steps_ok(self) -> float | None:
        if not self.steps_ok_n:
            return None
        return self.steps_ok_sum / self.steps_ok_n


def _fmt_pct(x: float) -> str:
    return f"{100.0 * x:6.2f}%"


def _infer_group(path: str) -> str:
    p = (path or "").strip().lstrip("/")
    if not p:
        return "(none)"
    return p.split("/")[0].upper()


def _level_key(path: str, *, depth: int) -> str:
    p = (path or "").strip().lstrip("/")
    if not p:
        return "(none)"
    parts = [x for x in p.split("/") if x]
    return "/".join(parts[: max(1, int(depth))])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("log", type=str, help="JSONL file produced by benchmark_runner")
    ap.add_argument(
        "--good-steps",
        type=int,
        default=None,
        help=(
            "If set, split successes into: within good-steps vs exceeded. "
            "Example: --good-steps 200"
        ),
    )
    ap.add_argument(
        "--by-depth",
        type=int,
        default=2,
        help="Group by the first N path segments (default: 2).",
    )
    ap.add_argument(
        "--min-episodes",
        type=int,
        default=1,
        help="Only show level buckets with at least this many episodes.",
    )
    ap.add_argument(
        "--sort",
        type=str,
        default="rate",
        choices=("rate", "n", "name"),
        help="Sort per-level rows by success rate / count / name.",
    )
    args = ap.parse_args()

    log_path = Path(args.log)
    if not log_path.exists():
        raise SystemExit(f"No such file: {log_path}")

    total = Agg()
    by_group: dict[str, Agg] = defaultdict(Agg)
    by_level: dict[str, Agg] = defaultdict(Agg)

    lines = 0
    bad = 0

    with log_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            lines += 1
            try:
                rec = json.loads(line)
            except Exception:
                bad += 1
                continue

            path = str(rec.get("path") or "")
            success = bool(rec.get("success"))
            steps = rec.get("steps")
            steps_i = int(steps) if isinstance(steps, int) else None

            total.add(success=success, steps=steps_i, good_steps=args.good_steps)
            g = _infer_group(path)
            by_group[g].add(success=success, steps=steps_i, good_steps=args.good_steps)
            lvl = _level_key(path, depth=args.by_depth)
            by_level[lvl].add(success=success, steps=steps_i, good_steps=args.good_steps)

    print(f"file: {log_path}")
    base_line = f"episodes: {total.n}  ok: {total.ok}  success: {_fmt_pct(total.rate())}"
    if args.good_steps is not None:
        base_line += f"  good(<= {args.good_steps}): {_fmt_pct(total.rate_good())}  late(> {args.good_steps}): {_fmt_pct(total.rate_late())}"
    ms = total.mean_steps_ok()
    if ms is not None:
        base_line += f"  mean_steps_ok: {ms:.2f}"
    print(base_line)
    if bad:
        print(f"WARN: could not parse {bad} lines as JSON")

    # Group summary
    print("\nBy group:")
    for g in sorted(by_group.keys()):
        a = by_group[g]
        line = f"  {g:10s}  n={a.n:6d}  ok={a.ok:6d}  success={_fmt_pct(a.rate())}"
        if args.good_steps is not None:
            line += f"  good={_fmt_pct(a.rate_good())}  late={_fmt_pct(a.rate_late())}"
        ms = a.mean_steps_ok()
        if ms is not None:
            line += f"  mean_steps_ok={ms:.2f}"
        print(line)

    # Level summary
    rows = [(k, v) for k, v in by_level.items() if v.n >= int(args.min_episodes)]
    if args.sort == "rate":
        rows.sort(key=lambda kv: (kv[1].rate(), kv[1].n), reverse=True)
    elif args.sort == "n":
        rows.sort(key=lambda kv: kv[1].n, reverse=True)
    else:
        rows.sort(key=lambda kv: kv[0])

    print(f"\nBy level (depth={args.by_depth}, min_episodes={args.min_episodes}, sort={args.sort}):")
    for k, a in rows:
        line = f"  {k:40s}  n={a.n:6d}  ok={a.ok:6d}  success={_fmt_pct(a.rate())}"
        if args.good_steps is not None:
            line += f"  good={_fmt_pct(a.rate_good())}  late={_fmt_pct(a.rate_late())}"
        ms = a.mean_steps_ok()
        if ms is not None:
            line += f"  mean_steps_ok={ms:.2f}"
        print(line)


if __name__ == "__main__":
    main()
