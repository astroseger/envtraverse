#!/usr/bin/env python3
"""Compose fixed-horizon MAP + ACT^h -> POS datasets from saved seq traces.

Each trace contributes at most one JSONL record.  For a requested horizon ``h``,
the record contains the initial rendered map and the first ``h`` actions; its
target is the position *after* the h-th action::

    {
      "input": "<MAP>...</MAP>\n<ACT>right</ACT>\n<ACT>down</ACT>",
      "target": "<POS>(1, 1)</POS>"
    }

The raw seq trace has the form ``MAP, POS_0, ACT_1, POS_1, ...``.  Thus, using
one-based action numbering, ``--horizon h`` predicts ``POS_h``.  The initial
``POS_0`` is not copied because it is already represented by the agent marker in
the initial map.

This is an endpoint-composition task rather than an autoregressive rollout: no
intermediate positions are exposed to the model, and train/validation use the
same input/target contract.  Trajectories are never deduplicated.  A trace that
has fewer than ``h`` non-terminal transitions is skipped.  The terminal
transition is excluded, consistently with the per-step composer, because its
out-of-bounds outcome is not fully represented by the ordinary in-maze state.

Compose train and validation traces separately, for example::

    python3 scripts/compose_seq_dataset.py --horizon 8 \
      --input-dir logs/dataset/seq/train \
      --output logs/dataset/composed/3x3-keys-rnd-h8-train.jsonl
    python3 scripts/compose_seq_dataset.py --horizon 8 \
      --input-dir logs/dataset/seq/valid \
      --output logs/dataset/composed/3x3-keys-rnd-h8-valid.jsonl

The default output is a gitignored artifact under logs/dataset/composed/.
Existing outputs are never replaced unless --overwrite is supplied.
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
DEFAULT_INPUT_DIR = ROOT / "logs" / "dataset" / "seq"
DEFAULT_OUTPUT = ROOT / "logs" / "dataset" / "composed" / "seq_map_actions_to_endpoint.jsonl"

# META is collection metadata, not part of the model stream. DOTALL preserves
# the ASCII map's newlines exactly.
META_RE = re.compile(r"<META>.*?</META>", re.DOTALL)
TAG_RE = re.compile(r"<(MAP|POS|ACT|END)>(.*?)</\1>", re.DOTALL)


class TraceFormatError(ValueError):
    """A seq trace does not follow the expected MAP/POS/(ACT/POS)* format."""


@dataclass(frozen=True)
class Example:
    input: str
    target: str


def _error(path: Path, message: str) -> TraceFormatError:
    return TraceFormatError(f"{path}: {message}")


def example_from_trace(path: Path, *, horizon: int) -> Example | None:
    """Build one fixed-horizon endpoint example, or None for a short trace."""

    if horizon < 1:
        raise ValueError("horizon must be at least 1")

    text = META_RE.sub("", path.read_text(encoding="utf-8"))
    map_text: str | None = None
    saw_initial_pos = False
    actions: list[str] = []
    positions: list[str] = []
    pending_action: str | None = None
    saw_end = False

    for match in TAG_RE.finditer(text):
        tag, value = match.group(1), match.group(2)

        if saw_end:
            raise _error(path, "contains a MAP, POS, ACT, or END block after END")

        if tag == "MAP":
            if map_text is not None:
                raise _error(path, "contains more than one MAP block")
            if saw_initial_pos or pending_action is not None or actions:
                raise _error(path, "MAP must appear before the initial POS and all actions")
            map_text = value
            continue

        if tag == "POS":
            if map_text is None:
                raise _error(path, "POS appears before MAP")
            if not saw_initial_pos:
                if pending_action is not None or actions:
                    raise _error(path, "initial POS appears after an action")
                if not value.strip():
                    raise _error(path, "initial POS is empty")
                saw_initial_pos = True
                continue
            if pending_action is None:
                raise _error(path, "POS appears without a preceding ACT")
            if not value.strip():
                raise _error(path, "target POS is empty")
            actions.append(pending_action)
            positions.append(value)
            pending_action = None
            continue

        if tag == "ACT":
            if map_text is None or not saw_initial_pos:
                raise _error(path, "ACT appears before MAP and initial POS")
            if pending_action is not None:
                raise _error(path, "two ACT blocks appear without an intervening POS")
            if not value.strip():
                raise _error(path, "ACT is empty")
            pending_action = value
            continue

        # tag == "END"
        if map_text is None or not saw_initial_pos:
            raise _error(path, "END appears before MAP and initial POS")
        if pending_action is not None:
            raise _error(path, "END appears before the preceding ACT has a target POS")
        if not actions:
            raise _error(path, "END appears before any complete transition")
        # The last position represents the out-of-bounds terminal state.
        actions.pop()
        positions.pop()
        saw_end = True

    if map_text is None:
        raise _error(path, "contains no MAP block")
    if not saw_initial_pos:
        raise _error(path, "contains no initial POS block")
    if pending_action is not None:
        raise _error(path, "final ACT has no target POS")
    if len(actions) < horizon:
        return None

    prefix_actions = actions[:horizon]
    return Example(
        input="\n".join([f"<MAP>{map_text}</MAP>", *[f"<ACT>{action}</ACT>" for action in prefix_actions]]),
        target=f"<POS>{positions[horizon - 1]}</POS>",
    )


def examples_from_trace(path: Path, *, horizon: int) -> Iterator[Example]:
    """Yield zero or one fixed-horizon example from a trace."""

    example = example_from_trace(path, horizon=horizon)
    if example is not None:
        yield example


def compose(input_dir: Path, output: Path, *, horizon: int, overwrite: bool) -> tuple[int, int]:
    """Write one endpoint example per sufficiently long trace, without deduplication."""

    if horizon < 1:
        raise ValueError("horizon must be at least 1")
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Input directory does not exist: {input_dir}")
    if output.exists() and not overwrite:
        raise FileExistsError(f"Output already exists: {output} (pass --overwrite to replace it)")

    traces = sorted(input_dir.rglob("*.trace.txt"))
    if not traces:
        raise FileNotFoundError(f"No *.trace.txt files found under: {input_dir}")

    output.parent.mkdir(parents=True, exist_ok=True)
    examples = 0
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        for trace in traces:
            for example in examples_from_trace(trace, horizon=horizon):
                json.dump(
                    {"input": example.input, "target": example.target},
                    stream,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                stream.write("\n")
                examples += 1

    return len(traces), examples


def positive_horizon(value: str) -> int:
    try:
        horizon = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("horizon must be an integer") from exc
    if horizon < 1:
        raise argparse.ArgumentTypeError("horizon must be at least 1")
    return horizon


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--horizon",
        type=positive_horizon,
        required=True,
        help="number of initial actions supplied; predict the resulting position",
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"raw seq trace directory (default: {DEFAULT_INPUT_DIR})",
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
    args = parser.parse_args()

    try:
        traces, examples = compose(
            args.input_dir,
            args.output,
            horizon=args.horizon,
            overwrite=args.overwrite,
        )
    except (FileNotFoundError, FileExistsError, TraceFormatError, ValueError) as exc:
        parser.error(str(exc))

    print(
        f"Wrote {examples} horizon-{args.horizon} sequence examples from {traces} traces "
        f"to {args.output}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
