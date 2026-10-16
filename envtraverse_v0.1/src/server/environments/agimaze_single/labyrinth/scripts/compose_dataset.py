#!/usr/bin/env python3
"""Compose a deduplicated per-step MAP + ACT -> POS dataset from saved traces.

Each JSONL record has exactly this schema::

    {"input": "<MAP>...</MAP>\n<ACT>right</ACT>",
     "target": "<POS>(0, 1)</POS>"}

The raw trace's META and END blocks are deliberately not copied into records.
Identical MAP + ACT -> POS records, which are common in random-walk traces, are
written once while retaining the first occurrence's deterministic trace order.
Pass ``--keep_duplicates`` to retain every example instead.

Usage:
    python3 scripts/compose_dataset.py
    python3 scripts/compose_dataset.py --output /path/to/per_step.jsonl

The default output is a gitignored artifact under logs/dataset/composed/.  Existing
outputs are never replaced unless --overwrite is supplied.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = ROOT / "logs" / "dataset" / "per_step"
DEFAULT_OUTPUT = ROOT / "logs" / "dataset" / "composed" / "per_step_map_act_to_pos.jsonl"

# META and END are intentionally absent: they are collection/filtering metadata,
# not part of the model stream. DOTALL preserves the ASCII map's newlines exactly.
META_RE = re.compile(r"<META>.*?</META>", re.DOTALL)
STEP_TAG_RE = re.compile(r"<(POS|MAP|ACT|END)>(.*?)</\1>", re.DOTALL)


class TraceFormatError(ValueError):
    """A per_step trace does not follow the expected POS/MAP/ACT sequence."""


@dataclass(frozen=True)
class Example:
    input: str
    target: str


def _error(path: Path, message: str) -> TraceFormatError:
    return TraceFormatError(f"{path}: {message}")


def examples_from_trace(path: Path) -> Iterator[Example]:
    """Yield complete MAP + ACT -> following POS transitions from one trace.

    A trace begins with POS, MAP, ACT and then alternates POS, MAP, ACT.  A final
    incomplete action (possible when collection stops mid-request) is ignored.

    If a trace terminates with ``<END>``, its immediately preceding transition is
    excluded.  The full engine state which determines whether an apparent border
    crossing actually exits is not represented in the per-step input, so that
    transition is not a deterministic MAP + ACT -> POS training example.
    Malformed ordering is rejected rather than silently mislabelling examples.
    """

    text = META_RE.sub("", path.read_text(encoding="utf-8"))
    current_map: str | None = None
    pending_action: str | None = None
    examples: list[Example] = []
    saw_pos = False
    saw_end = False

    for match in STEP_TAG_RE.finditer(text):
        tag, value = match.group(1), match.group(2)

        if saw_end:
            raise _error(path, "contains a POS, MAP, or ACT block after END")

        if tag == "END":
            if not value.strip():
                raise _error(path, "END is empty")
            if pending_action is not None:
                raise _error(path, "END appears before the preceding ACT has a target POS")
            if not examples:
                raise _error(path, "END appears before any complete transition")
            examples.pop()
            saw_end = True
            continue

        if tag == "POS":
            if pending_action is not None:
                if current_map is None:  # Defensive; impossible after ACT validation.
                    raise _error(path, "ACT has no preceding MAP")
                examples.append(
                    Example(
                        input=f"<MAP>{current_map}</MAP>\n<ACT>{pending_action}</ACT>",
                        target=f"<POS>{value}</POS>",
                    )
                )
                # The target POS starts the next state; its MAP follows next.
                pending_action = None
                current_map = None
            elif current_map is not None:
                raise _error(path, "POS appears where a MAP was expected")
            saw_pos = True
            continue

        if tag == "MAP":
            if not saw_pos:
                raise _error(path, "MAP appears before the initial POS")
            if pending_action is not None:
                raise _error(path, "MAP appears before the preceding ACT has a target POS")
            if current_map is not None:
                raise _error(path, "two MAP blocks appear without an intervening ACT")
            current_map = value
            continue

        # tag == "ACT"
        if current_map is None:
            raise _error(path, "ACT appears before a MAP")
        if pending_action is not None:
            raise _error(path, "two ACT blocks appear without an intervening target POS")
        if not value.strip():
            raise _error(path, "ACT is empty")
        # Keep this map until the following POS is emitted as the target.
        pending_action = value

    if not saw_pos:
        raise _error(path, "contains no POS block")

    yield from examples


def compose(
    input_dir: Path, output: Path, *, overwrite: bool, keep_duplicates: bool = False
) -> tuple[int, int]:
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Input directory does not exist: {input_dir}")
    if output.exists() and not overwrite:
        raise FileExistsError(f"Output already exists: {output} (pass --overwrite to replace it)")

    traces = sorted(input_dir.rglob("*.trace.txt"))
    if not traces:
        raise FileNotFoundError(f"No *.trace.txt files found under: {input_dir}")

    output.parent.mkdir(parents=True, exist_ok=True)
    examples = 0
    seen: set[Example] = set()
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        for trace in traces:
            for example in examples_from_trace(trace):
                if not keep_duplicates:
                    if example in seen:
                        continue
                    seen.add(example)
                json.dump(
                    {"input": example.input, "target": example.target},
                    stream,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                stream.write("\n")
                examples += 1

    return len(traces), examples


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"raw per_step trace directory (default: {DEFAULT_INPUT_DIR})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"JSONL dataset path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing output file",
    )
    parser.add_argument(
        "--keep_duplicates",
        action="store_true",
        help="retain duplicate MAP + ACT -> POS examples",
    )
    args = parser.parse_args()

    try:
        traces, examples = compose(
            args.input_dir,
            args.output,
            overwrite=args.overwrite,
            keep_duplicates=args.keep_duplicates,
        )
    except (FileNotFoundError, FileExistsError, TraceFormatError) as exc:
        parser.error(str(exc))

    qualifier = "" if args.keep_duplicates else " unique"
    print(f"Wrote {examples}{qualifier} examples from {traces} traces to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
