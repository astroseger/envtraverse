"""Fetch pinned official benchmark checkouts and list combined game IDs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any
from uuid import uuid4

from .catalog import BenchmarkCatalog, DATASETS, add_catalog_arguments, catalog_from_args
from .records import atomic_json, now


def git(directory: Path | None, *arguments: str) -> str:
    argv = ["git"]
    if directory is not None:
        argv += ["-C", str(directory)]
    result = subprocess.run(argv + list(arguments), text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise RuntimeError(f"Git failed ({' '.join(arguments[:2])}): {result.stderr.strip()}")
    return result.stdout.strip()


def _games(root: Path, benchmark: str) -> list[str]:
    definition = DATASETS[benchmark]
    catalog = BenchmarkCatalog(**{definition.root_option: root})
    return [g for g in catalog.game_ids() if g.startswith(definition.prefix)]


def fetch(destination: Path, ref: str | None = None, *, repository: str | None = None,
          benchmark: str = "terminal-bench-2.1") -> dict[str, Any]:
    """Never update an existing checkout implicitly. repository permits offline tests.

    New 3.0/4.0/Science downloads resolve their release tag, not the moving default branch.
    Explicit refs are operator overrides and are recorded as such in the lock.
    """
    if benchmark not in DATASETS:
        raise ValueError("Unsupported benchmark")
    definition = DATASETS[benchmark]
    repository = repository or definition.repository
    destination = destination.absolute()
    if ref is not None and (not ref or ref.startswith("-") or any(c.isspace() for c in ref)):
        raise ValueError("ref must be a commit, branch, or tag without whitespace/options")
    requested_ref = ref if ref is not None else definition.default_ref
    # Disambiguate our defaults as tags; never accept a same-named branch instead.
    target = ref or ("refs/tags/" + definition.default_ref if definition.default_ref else "HEAD")
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock = destination.parent / (destination.name + ".lock.json")
    if destination.exists():
        if not (destination / ".git").exists():
            raise RuntimeError("Destination exists but is not a Git checkout; refusing to overwrite it")
        if git(destination, "remote", "get-url", "origin") != repository:
            raise RuntimeError("Existing checkout has a different origin; use a new destination")
        if git(destination, "status", "--porcelain", "--untracked-files=normal"):
            raise RuntimeError("Existing checkout has local changes; refusing to repin it")
        commit = git(destination, "rev-parse", "HEAD")
        if lock.exists():
            record = json.loads(lock.read_text(encoding="utf-8"))
            if (not isinstance(record, dict) or record.get("benchmark") != benchmark or
                    record.get("repository") != repository):
                raise RuntimeError("Checkout lock belongs to a different benchmark/repository")
            if record.get("revision") != commit:
                raise RuntimeError("Checkout differs from its lock record; use a fresh destination")
            if ref is not None and git(destination, "rev-parse", "--verify", ref + "^{commit}") != commit:
                raise RuntimeError("Requested revision differs from existing checkout; use a new destination")
            # A lock is authoritative on repeat setup (including an earlier explicit ref).
            game_ids = _games(destination, benchmark)
            if not game_ids:
                raise RuntimeError("No Harbor tasks found in the downloaded checkout")
            destination.with_name(destination.name + ".game-ids.txt").write_text(
                "\n".join(game_ids) + "\n", encoding="utf-8")
            return record
        # Adopting a checkout without a lock requires verifying its selected release.
        if requested_ref is not None and git(destination, "rev-parse", "--verify", target + "^{commit}") != commit:
            raise RuntimeError("Requested revision differs from existing checkout; use a new destination")
    else:
        # Do not destroy provenance if a checkout was removed but its lock remains.
        if lock.exists():
            raise RuntimeError("Lock exists without its checkout; restore it or choose a new destination")
        temporary = destination.with_name("." + destination.name + ".download-" + uuid4().hex)
        try:
            git(None, "clone", "--", repository, str(temporary))
            try:
                commit = git(temporary, "rev-parse", "--verify", target + "^{commit}")
            except RuntimeError:
                git(temporary, "fetch", "origin", target)
                commit = git(temporary, "rev-parse", "FETCH_HEAD^{commit}")
            git(temporary, "checkout", "--detach", commit)
            if not (temporary / "tasks").is_dir():
                raise RuntimeError("Downloaded repository does not contain tasks/")
            if not _games(temporary, benchmark):
                raise RuntimeError("No Harbor tasks found in the downloaded checkout")
            temporary.rename(destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    game_ids = _games(destination, benchmark)
    if not game_ids:
        raise RuntimeError("No Harbor tasks found in the downloaded checkout")
    record = {"benchmark": benchmark, "repository": repository,
              "revision": commit, "requested_ref": requested_ref or "HEAD",
              "default_release_ref": definition.default_ref, "explicit_ref_override": ref is not None,
              "task_count": len(game_ids), "recorded_at": now()}
    atomic_json(lock, record)
    destination.with_name(destination.name + ".game-ids.txt").write_text(
        "\n".join(game_ids) + "\n", encoding="utf-8")
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    download = sub.add_parser("fetch", help="Download a benchmark and record its exact commit")
    download.add_argument("--benchmark", choices=list(DATASETS), default="terminal-bench-2.1")
    download.add_argument("--destination", type=Path, required=True)
    download.add_argument("--ref", help="Override initial revision; defaults: TB3 v3.0.0, TB4 v4.0.0, Science v0.1.0; TB2.1 HEAD")
    listing = sub.add_parser("list", help="Print game IDs in the configured checkouts")
    add_catalog_arguments(listing)
    listing.add_argument("--benchmark", choices=["smoke-test", *DATASETS])
    listing.add_argument("--format", choices=["lines", "yaml"], default="lines")
    args = parser.parse_args()
    try:
        if args.command == "fetch":
            print(json.dumps(fetch(args.destination, args.ref, benchmark=args.benchmark), indent=2))
        else:
            catalog = catalog_from_args(args)
            if args.benchmark in DATASETS and args.benchmark not in catalog.roots:
                raise RuntimeError(f"{args.benchmark} is not configured; run setup or supply its root")
            games = [g for g in catalog.game_ids() if args.benchmark is None or
                     catalog.resolve(g).benchmark == args.benchmark]
            if args.format == "yaml":
                print("games:\n" + "\n".join("  - " + game for game in games) if games else "games: []")
            else:
                print("\n".join(games))
    except (RuntimeError, ValueError, OSError) as exc:
        parser.exit(1, f"Dataset operation failed: {exc}\n")


if __name__ == "__main__":
    main()
