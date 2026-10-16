#!/usr/bin/env python3
"""Join saved ``seq`` maps with matching public-text traces into JSONL.

The input directory must contain sibling ``seq/`` and ``txt/`` directories.  For
any ``txt/<relative-path>/<session>.trace.txt``, the composer reads the matching
``seq/<relative-path>/<session>.trace.txt`` and inserts that trace's single
``<MAP>...</MAP>`` block in place of the txt trace's collection-only
``<META>`` block, immediately before ``<START>``.  The rest of the txt trace
is retained unchanged in semantic content.

One completed trace is emitted per JSONL record::

    {"trace":"<MAP>...</MAP>\\n<START>...</START>\\n..."}

This script deliberately does *not* turn a trace into input/target pairs, crop
it to a rollout depth, filter tags, or deduplicate records.  Those choices
belong to the training data loader.  With ``--check-actions``, it additionally
checks that the ordered ``<ACT>`` payloads agree between each txt trace and its
matching seq trace.

Examples::

    python3 scripts/compose_txt_dataset.py \\
      --input-dir logs/dataset/train \\
      --output logs/dataset/composed/train-txt-with-map.jsonl

    python3 scripts/compose_txt_dataset.py \\
      --input-dir logs/dataset/train --check-actions \\
      --output logs/dataset/composed/train-txt-with-map.jsonl

Existing outputs are never replaced unless ``--overwrite`` is supplied.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = ROOT / "logs" / "dataset"
DEFAULT_OUTPUT = ROOT / "logs" / "dataset" / "composed" / "txt_with_map.jsonl"

META_RE = re.compile(r"<META>.*?</META>", re.DOTALL)
MAP_RE = re.compile(r"<MAP>.*?</MAP>", re.DOTALL)
START_RE = re.compile(r"<START>.*?</START>", re.DOTALL)
ACT_RE = re.compile(r"<ACT>(.*?)</ACT>", re.DOTALL)
TRACE_SUFFIX = ".trace.txt"


class TraceFormatError(ValueError):
    """A raw seq or txt trace cannot be composed safely."""


def _error(path: Path, message: str) -> TraceFormatError:
    return TraceFormatError(f"{path}: {message}")


def _only_match(pattern: re.Pattern[str], text: str, *, label: str, path: Path) -> re.Match[str]:
    matches = list(pattern.finditer(text))
    if not matches:
        raise _error(path, f"contains no {label} block")
    if len(matches) != 1:
        raise _error(path, f"contains {len(matches)} {label} blocks; expected exactly one")
    return matches[0]


def _actions(text: str) -> list[str]:
    return [match.group(1).strip() for match in ACT_RE.finditer(text)]


def compose_trace(txt_path: Path, seq_path: Path, *, check_actions: bool = False) -> str:
    """Return one txt trace with META removed and the matching seq map prepended."""

    txt_text = txt_path.read_text(encoding="utf-8")
    seq_text = seq_path.read_text(encoding="utf-8")

    meta_match = _only_match(META_RE, txt_text, label="META", path=txt_path)
    start_match = _only_match(START_RE, txt_text, label="START", path=txt_path)
    if meta_match.end() > start_match.start():
        raise _error(txt_path, "META must appear before START")
    if MAP_RE.search(txt_text):
        raise _error(txt_path, "already contains a MAP block")

    map_match = _only_match(MAP_RE, seq_text, label="MAP", path=seq_path)

    if check_actions:
        txt_actions = _actions(txt_text)
        seq_actions = _actions(seq_text)
        if txt_actions != seq_actions:
            raise _error(
                txt_path,
                f"ACT sequence differs from matching seq trace {seq_path}",
            )

    # META is collection-only metadata and must not enter the model stream.
    # Trace writers use a newline between top-level blocks.  Normalize only the
    # newly created MAP/START boundary; all remaining txt content stays intact.
    map_block = map_match.group(0).strip("\n")
    after = txt_text[meta_match.end() :].lstrip("\n")
    return f"{map_block}\n{after}"


def compose(
    input_dir: Path,
    output: Path,
    *,
    overwrite: bool,
    check_actions: bool = False,
) -> tuple[int, int]:
    """Write one map-augmented txt trace record for every txt trace."""

    seq_dir = input_dir / "seq"
    txt_dir = input_dir / "txt"
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Input directory does not exist: {input_dir}")
    if not seq_dir.is_dir():
        raise FileNotFoundError(f"seq directory does not exist: {seq_dir}")
    if not txt_dir.is_dir():
        raise FileNotFoundError(f"txt directory does not exist: {txt_dir}")
    if output.exists() and not overwrite:
        raise FileExistsError(f"Output already exists: {output} (pass --overwrite to replace it)")

    txt_traces = sorted(txt_dir.rglob(f"*{TRACE_SUFFIX}"))
    if not txt_traces:
        raise FileNotFoundError(f"No *{TRACE_SUFFIX} files found under: {txt_dir}")

    output.parent.mkdir(parents=True, exist_ok=True)
    records = 0
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        for txt_path in txt_traces:
            relative_path = txt_path.relative_to(txt_dir)
            seq_path = seq_dir / relative_path
            if not seq_path.is_file():
                raise FileNotFoundError(
                    f"Matching seq trace does not exist for {txt_path}: {seq_path}"
                )
            trace = compose_trace(txt_path, seq_path, check_actions=check_actions)
            json.dump({"trace": trace}, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            records += 1

    return len(txt_traces), records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"directory containing seq/ and txt/ subdirectories (default: {DEFAULT_INPUT_DIR})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"map-augmented txt JSONL path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing output file",
    )
    parser.add_argument(
        "--check-actions",
        action="store_true",
        help="require matching ordered ACT payloads in each txt/seq trace pair",
    )
    args = parser.parse_args()

    try:
        traces, records = compose(
            args.input_dir,
            args.output,
            overwrite=args.overwrite,
            check_actions=args.check_actions,
        )
    except (FileNotFoundError, FileExistsError, TraceFormatError) as exc:
        parser.error(str(exc))

    print(f"Wrote {records} map-augmented txt traces from {traces} txt traces to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
