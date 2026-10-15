"""Discover local environment packages without importing their dependencies."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import cast

from environment_api import EnvironmentManager


ENVIRONMENTS_DIR = Path(__file__).resolve().parent / "environments"


def available_environments() -> list[str]:
    return sorted(
        path.name
        for path in ENVIRONMENTS_DIR.iterdir()
        if path.is_dir()
        and path.name.isidentifier()
        and path.resolve().parent == ENVIRONMENTS_DIR
        and (path / "environment_manager.py").is_file()
    )


def load_environment_manager(name: str) -> type[EnvironmentManager]:
    available = available_environments()
    if name not in available:
        raise ValueError(f"Unknown environment {name!r}. Available environments: {', '.join(available)}")
    try:
        module = import_module(f"environments.{name}.environment_manager")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            f"Cannot load environment {name!r}: missing Python module {exc.name!r}. "
            f"Check the dependencies documented in {ENVIRONMENTS_DIR / name}."
        ) from exc
    manager_class = getattr(module, "EnvironmentManager", None)
    if not isinstance(manager_class, type) or any(
        not callable(getattr(manager_class, method, None))
        for method in ("start_environment", "move", "stop")
    ):
        raise TypeError(
            f"Environment {name!r} must export an EnvironmentManager class with "
            "start_environment(), move(), and stop()."
        )
    return cast(type[EnvironmentManager], manager_class)
