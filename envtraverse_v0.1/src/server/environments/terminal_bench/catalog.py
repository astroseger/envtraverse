"""Resolve explicitly supported, versioned benchmark names to local Harbor tasks."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import tomllib
from typing import Any

from .protocol import RuntimeFault

TB21_REPOSITORY = "https://github.com/harbor-framework/terminal-bench-2-1.git"
TB_REPOSITORY = "https://github.com/harbor-framework/terminal-bench.git"
TBSCIENCE_REPOSITORY = "https://github.com/harbor-framework/terminal-bench-science.git"
SMOKE_GAME = "smoke-test-smoke-test"
TB21_PREFIX = "terminal-bench-2.1-"
TB3_PREFIX = "terminal-bench-3.0-"
TB4_PREFIX = "terminal-bench-4.0-"
BUILTIN_SMOKE = Path(__file__).resolve().parent / "fixtures" / "smoke-test"
TASK_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


@dataclass(frozen=True)
class DatasetDefinition:
    name: str
    repository: str
    default_ref: str | None
    root_option: str
    manifest_name: str | None = None

    @property
    def prefix(self) -> str:
        return self.name + "-"


DATASETS = {
    "terminal-bench-2.1": DatasetDefinition("terminal-bench-2.1", TB21_REPOSITORY, None, "tb21_root"),
    "terminal-bench-3.0": DatasetDefinition("terminal-bench-3.0", TB_REPOSITORY, "v3.0.0", "tb3_root"),
    "terminal-bench-4.0": DatasetDefinition("terminal-bench-4.0", TB_REPOSITORY, "v4.0.0", "tb4_root"),
    "terminal-bench-science-0.1": DatasetDefinition(
        "terminal-bench-science-0.1", TBSCIENCE_REPOSITORY, "v0.1.0", "tbscience_root",
        "terminal-bench-science/terminal-bench-science"),
}


def split_game_id(value: Any) -> tuple[str, str]:
    if value == SMOKE_GAME:
        return "smoke-test", "smoke-test"
    if isinstance(value, str):
        for name, definition in DATASETS.items():
            if value.startswith(definition.prefix):
                task = value[len(definition.prefix):]
                if all(TASK_COMPONENT.fullmatch(part) for part in task.split("/")):
                    return name, task
                break
    raise RuntimeFault("unknown_task", "Use smoke-test-smoke-test or " +
                       ", ".join(d.prefix + "<task-path>" for d in DATASETS.values()))


def _manifest_tasks(root: Path, expected_name: str) -> set[str]:
    """Read the published task allowlist, whose package names use leaf slugs."""
    path = root / "tasks" / "dataset.toml"
    manifest = tomllib.loads(path.read_text(encoding="utf-8"))
    dataset, entries = manifest.get("dataset"), manifest.get("tasks")
    if (not isinstance(dataset, dict) or dataset.get("name") != expected_name
            or not isinstance(entries, list) or not entries):
        raise ValueError(f"{path} must describe {expected_name} and its tasks")
    prefix = expected_name.split("/", 1)[0] + "/"
    tasks: set[str] = set()
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        if (not isinstance(name, str) or not name.startswith(prefix)
                or not TASK_COMPONENT.fullmatch(name[len(prefix):])):
            raise ValueError(f"Invalid task name in {path}")
        slug = name[len(prefix):]
        if slug in tasks:
            raise ValueError(f"Duplicate task {name} in {path}")
        tasks.add(slug)
    return tasks


@dataclass(frozen=True)
class TaskSpec:
    game_id: str
    benchmark: str
    task_name: str
    path: Path
    source: dict[str, Any]


class BenchmarkCatalog:
    def __init__(self, tb21_root: Path | None = None, *, tb3_root: Path | None = None,
                 tb4_root: Path | None = None, tbscience_root: Path | None = None,
                 smoke_task: Path = BUILTIN_SMOKE):
        # All roots are independent repository checkouts containing tasks/.
        self.smoke_task = smoke_task.resolve(strict=True)
        self.roots: dict[str, Path] = {}
        self.sources: dict[str, dict[str, Any]] = {}
        self._members: dict[str, set[str]] = {}
        configured = {"terminal-bench-2.1": tb21_root, "terminal-bench-3.0": tb3_root,
                      "terminal-bench-4.0": tb4_root, "terminal-bench-science-0.1": tbscience_root}
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
            if manifest_name := DATASETS[name].manifest_name:
                self._members[name] = _manifest_tasks(root, manifest_name)
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
            if not path.is_relative_to(root):
                raise RuntimeFault("unknown_task", "Task path is outside the benchmark directory")
            source = self.sources[benchmark]
        if not path.is_dir() or not (path / "task.toml").is_file() or not (path / "instruction.md").is_file():
            raise RuntimeFault("unknown_task", "Unknown task in the selected benchmark")
        if benchmark in self._members:
            slug = name.rsplit("/", 1)[-1]
            if slug not in self._members[benchmark]:
                raise RuntimeFault("unknown_task", "Task is not in the published dataset manifest")
            manifest_name = DATASETS[benchmark].manifest_name
            assert manifest_name is not None
            package = tomllib.loads((path / "task.toml").read_text(encoding="utf-8")).get("task")
            if (not isinstance(package, dict)
                    or package.get("name") != manifest_name.split("/", 1)[0] + "/" + slug):
                raise RuntimeFault("unknown_task", "Task package name does not match its manifest entry")
        return TaskSpec(game_id, benchmark, name, path, dict(source))

    def game_ids(self) -> list[str]:
        names = [SMOKE_GAME]
        for benchmark, root in self.roots.items():
            tasks_root = root / "tasks"
            members = self._members.get(benchmark)
            found: set[str] = set()
            for config in sorted(tasks_root.rglob("task.toml")):
                relative = config.parent.relative_to(tasks_root).as_posix()
                candidate = DATASETS[benchmark].prefix + relative
                try:
                    self.resolve(candidate)
                except RuntimeFault:
                    continue
                if members is not None:
                    slug = config.parent.name
                    if slug in found:
                        raise ValueError(f"Duplicate manifest task {slug} in {benchmark}")
                    found.add(slug)
                names.append(candidate)
            if members is not None and (missing := members - found):
                raise ValueError(f"Missing manifest tasks in {benchmark}: {', '.join(sorted(missing))}")
        return names


def add_catalog_arguments(parser: Any) -> None:
    for definition in DATASETS.values():
        parser.add_argument("--" + definition.root_option.replace("_", "-"), type=Path,
                            help=f"{definition.name} repository root (contains tasks/)")


def catalog_from_args(args: Any) -> BenchmarkCatalog:
    return BenchmarkCatalog(**{d.root_option: getattr(args, d.root_option) for d in DATASETS.values()})
