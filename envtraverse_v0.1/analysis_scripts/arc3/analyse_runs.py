#!/usr/bin/env python3
"""Extract raw token counts and generate ARC3 scorecards by replay."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
ANALYSIS_SCRIPTS = ("extract_raw_cost_tokens.py", "arc3/generate_scorecards_by_replay.py")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_folder", type=Path, help="Run directory to analyse")
    args = parser.parse_args(argv)

    try:
        run_folder = args.run_folder.expanduser().resolve()
        for script in ANALYSIS_SCRIPTS:
            print(f"Running {script}...", flush=True)
            subprocess.run([sys.executable, str(SCRIPTS_DIR / script), str(run_folder)], check=True)
    except subprocess.CalledProcessError as exc:
        print(f"Error: {script} failed with exit code {exc.returncode}", file=sys.stderr)
        return exc.returncode if exc.returncode > 0 else 1
    except OSError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
