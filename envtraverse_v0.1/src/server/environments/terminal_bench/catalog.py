"""Resolve explicitly supported, versioned benchmark names to local Harbor tasks."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from .protocol import RuntimeFault

TB21_REPOSITORY = "https://github.com/harbor-framework/terminal-bench-2-1.git"
TB_REPOSITORY = "https://github.com/harbor-framework/terminal-bench.git"
SMOKE_GAME = "smoke-test-smoke-test"
TB21_PREFIX = "terminal-bench-2.1-"
TB3_PREFIX = "terminal-bench-3.0-"
TB4_PREFIX = "terminal-bench-4.0-"
BUILTIN_SMOKE = Path(__file__).resolve().parent / "fixtures" / "smoke-test"


@dataclass(frozen=True)
class DatasetDefinition:
    name: str
    repository: str
    default_ref: str | None
    root_option: str

    @property
    def prefix(self) -> str:
        return self.name + "-"


DATASETS = {
    "terminal-bench-2.1": DatasetDefinition("terminal-bench-2.1", TB21_REPOSITORY, None, "tb21_root"),
    "terminal-bench-3.0": DatasetDefinition("terminal-bench-3.0", TB_REPOSITORY, "v3.0.0", "tb3_root"),
    "terminal-bench-4.0": DatasetDefinition("terminal-bench-4.0", TB_REPOSITORY, "v4.0.0", "tb4_root"),
}


def split_game_id(value: Any) -> tuple[str, str]:
    if value == SMOKE_GAME:
        return "smoke-test", "smoke-test"
    if isinstance(value, str):
        for name, definition in DATASETS.items():
            if value.startswith(definition.prefix):
                task = value[len(definition.prefix):]
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", task):
                    return name, task
                break
    raise RuntimeFault("unknown_task", "Use smoke-test-smoke-test or " +
                       ", ".join(d.prefix + "<task-name>" for d in DATASETS.values()))


@dataclass(frozen=True)
class TaskSpec:
    game_id: str
    benchmark: str
    task_name: str
    path: Path
    source: dict[str, Any]


class BenchmarkCatalog:
    def __init__(self, tb21_root: Path | None = None, *, tb3_root: Path | None = None,
                 tb4_root: Path | None = None, smoke_task: Path = BUILTIN_SMOKE):
        # All roots are independent repository checkouts containing tasks/.
        self.smoke_task = smoke_task.resolve(strict=True)
        self.roots: dict[str, Path] = {}
        self.sources: dict[str, dict[str, Any]] = {}
        configured = {"terminal-bench-2.1": tb21_root, "terminal-bench-3.0": tb3_root,
                      "terminal-bench-4.0": tb4_root}
        for name, configured_root in configured.items():
            if configured_root is None:
                continue
            root = configured_root.resolve(strict=True)
            if root in self.roots.values():
                raise ValueError("Each benchmark version must have its own checkout directory")
            if not (root / "tasks").is_dir():
                option = DATASETS[name].root_option.replace("_", "-")
                raise ValueError(f"--{option} must contain a tasks/ directory")
            source = {"benchmark": name, "repository": DATASETS[name].repository, "revision": None,
                      "note": "Unregistered checkout; version/revision not verified by setup"}
            lock = root.parent / (root.name + ".lock.json")
            if lock.is_file():
                source = json.loads(lock.read_text(encoding="utf-8"))
                if not isinstance(source, dict) or source.get("benchmark") != name:
                    raise ValueError(f"Checkout lock belongs to a different benchmark than {name}")
            self.roots[name] = root
            self.sources[name] = source
        # Preserve the v0.2 programmatic attribute for existing callers.
        self.tb21_root = self.roots.get("terminal-bench-2.1")

    def resolve(self, game_id: Any) -> TaskSpec:
        benchmark, name = split_game_id(game_id)
        if benchmark == "smoke-test":
            path = self.smoke_task
            source = {"kind": "bundled synthetic integration fixture", "official_benchmark": False}
        else:
            if benchmark not in self.roots:
                raise RuntimeFault("unknown_task", f"{benchmark} is not configured; run setup first")
            root = (self.roots[benchmark] / "tasks").resolve()
            path = (root / name).resolve()
            if path.parent != root:
                raise RuntimeFault("unknown_task", "Task path is outside the benchmark directory")
            source = self.sources[benchmark]
        if not path.is_dir() or not (path / "task.toml").is_file() or not (path / "instruction.md").is_file():
            raise RuntimeFault("unknown_task", "Unknown task in the selected benchmark")
        return TaskSpec(game_id, benchmark, name, path, dict(source))

    def game_ids(self) -> list[str]:
        names = [SMOKE_GAME]
        for benchmark, root in self.roots.items():
            for directory in sorted((root / "tasks").iterdir()):
                if directory.is_dir():
                    candidate = DATASETS[benchmark].prefix + directory.name
                    try:
                        self.resolve(candidate)
                    except RuntimeFault:
                        continue
                    names.append(candidate)
        return names


def add_catalog_arguments(parser: Any) -> None:
    for definition in DATASETS.values():
        parser.add_argument("--" + definition.root_option.replace("_", "-"), type=Path,
                            help=f"{definition.name} repository root (contains tasks/)")


def catalog_from_args(args: Any) -> BenchmarkCatalog:
    return BenchmarkCatalog(**{d.root_option: getattr(args, d.root_option) for d in DATASETS.values()})
